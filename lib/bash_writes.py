"""
bash_writes — find the files a Bash command string provably WRITES, for the lead's Bash write gate
(backlog row 59, hooks/pretool_bash_gate.py).

Two layers:

- `parse_write_targets(cmd)` is PURE: string in, `[(path, est_lines)]` out, no filesystem, no git.
  It recognises redirect targets (`>`, `>>`, `>|`, `&>`, `&>>`), `tee [-a] FILE…`, `sed -i … FILE…`
  (GNU and BSD spellings), `cp`/`mv` destinations, `python - <<EOF` / `python3 -c CODE` bodies that
  `open(<literal>, "w"|"a"|"x")` or `Path(<literal>).write_text(...)`. `est_lines` is the line count
  of the heredoc body feeding that write (directly, or through a `|` pipeline), else None
  (unknown). A shape it does not recognise produces nothing — fail-open toward allow.

- `gated_targets(cmd, cwd, state_root, config, is_gate_exempt)` adds the I/O the hook needs: resolves each target
  against the session cwd, drops anything outside it, exempt (`lead_guard.is_gate_exempt`, any
  `_staging/` component), untracked scratch, or not in a git repo at all, then applies the Edit
  gate's rule: new file → gated (when `block_on_new_file`); known line count ≥
  `edit_line_threshold` → gated; unknown count on an existing file → allowed.

Nothing here raises: any error degrades to "no targets" (allow).
"""
import os
import re
import shlex
import subprocess
from pathlib import Path

# ---- pure parser -------------------------------------------------------------------------------

_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\2")
_PUNCT = ";&|<>()\n"
_EXTRA_WORDCHARS = ":,@%+{}$[]!^"
_SEPARATOR_CHARS = set(";&|\n()")
_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
_OTHER_REDIRECTS = {"<", "<>", ">&", "<&", ">>&", "&>&", "<<<"}
_PREFIX_CMDS = {"sudo", "command", "env", "nohup", "time", "exec", "builtin"}
_PY_RE = re.compile(r"^python(\d+(\.\d+)*)?$")
_PY_OPEN_RE = re.compile(
    r"""\bopen\(\s*[rRbB]?(['"])([^'"\n]+)\1\s*,\s*(?:mode\s*=\s*)?[rRbB]?(['"])([^'"\n]*)\3""")
_PY_WRITE_TEXT_RE = re.compile(
    r"""\bPath\(\s*[rR]?(['"])([^'"\n]+)\1\s*\)\s*\.\s*write_(?:text|bytes)\(""")
_SED_FLAG_OPTS = set("nrEsuz")


def _split_heredocs(cmd):
    """(script, bodies): the command text with every heredoc BODY (and its terminator line)
    removed, plus `(body_text, line_count)` per heredoc in the order their `<<` operators appear."""
    lines = cmd.split("\n")
    script_lines, bodies = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        script_lines.append(line)
        i += 1
        for m in _HEREDOC_RE.finditer(line):
            strip_tabs = m.group(1) == "-"
            delim = m.group(3)
            body = []
            while i < len(lines):
                cand = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if cand == delim:
                    break
                body.append(lines[i - 1])
            bodies.append(("\n".join(body), len(body)))
    return "\n".join(script_lines), bodies


def _tokens(script):
    lex = shlex.shlex(script, posix=True, punctuation_chars=_PUNCT)
    lex.whitespace = " \t\r"
    lex.wordchars += _EXTRA_WORDCHARS
    lex.commenters = ""
    return list(lex)


def _is_separator(tok):
    return bool(tok) and all(c in _SEPARATOR_CHARS for c in tok)


def _simple_commands(tokens, bodies):
    """A dict {words, writes:[target], heredoc:(text, n)|None, stdin:(text, n)|None} per simple
    command; `stdin` is its own heredoc or one fed to it through a `|` pipeline. Heredoc bodies are
    consumed in `<<` order."""
    body_iter = iter(bodies)
    cur = {"words": [], "writes": [], "heredoc": None}
    out = []
    inherited = None  # heredoc feeding this command through a pipe
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _is_separator(tok):
            cur["stdin"] = cur["heredoc"] or inherited
            out.append(cur)
            inherited = cur["stdin"] if tok == "|" else None
            cur = {"words": [], "writes": [], "heredoc": None}
            i += 1
            continue
        if tok in ("<<", "<<-"):
            cur["heredoc"] = next(body_iter, ("", 0))
            i += 2  # skip the delimiter word
            continue
        if tok in _WRITE_REDIRECTS:
            if i + 1 < len(tokens) and not _is_separator(tokens[i + 1]) \
                    and tokens[i + 1] not in _WRITE_REDIRECTS | _OTHER_REDIRECTS:
                cur["writes"].append(tokens[i + 1])
            i += 2
            continue
        if tok in _OTHER_REDIRECTS or (tok and set(tok) <= set("<>&")):
            i += 2  # the operator and its operand (a file, fd number or here-string)
            continue
        cur["words"].append(tok)
        i += 1
    cur["stdin"] = cur["heredoc"] or inherited
    out.append(cur)
    # a trailing fd number glued before a redirect (`2> err`) lands in words — harmless
    return out


def _program(words):
    """(basename, args) after stripping VAR=val assignments and prefix commands."""
    idx = 0
    while idx < len(words):
        w = words[idx]
        if re.match(r"^[A-Za-z_]\w*=", w):
            idx += 1
            continue
        if w in _PREFIX_CMDS:
            idx += 1
            # skip that prefix command's own options (`sudo -u x` is not worth modelling)
            while idx < len(words) and words[idx].startswith("-"):
                idx += 1
            continue
        break
    if idx >= len(words):
        return None, []
    return os.path.basename(words[idx]), words[idx + 1:]


def _sed_files(args):
    in_place = False
    script_given = False
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest.extend(args[i + 1:])
            break
        if a in ("-i", "--in-place"):
            in_place = True
            # BSD `-i ''` / `-i .bak` takes a separate suffix argument
            if i + 1 < len(args) and (args[i + 1] == "" or re.match(r"^\.\w*$", args[i + 1])):
                i += 1
        elif a.startswith("--in-place="):
            in_place = True
        elif a.startswith("-i") and len(a) > 2:
            in_place = True
        elif a in ("-e", "--expression", "-f", "--file"):
            script_given = True
            i += 1
        elif a.startswith("--expression=") or a.startswith("--file="):
            script_given = True
        elif a.startswith("-") and len(a) > 1 and set(a[1:]) <= _SED_FLAG_OPTS | {"i"}:
            if "i" in a[1:]:
                in_place = True
        elif a.startswith("-") and len(a) > 1:
            pass
        else:
            rest.append(a)
        i += 1
    if not in_place:
        return []
    if not script_given:
        rest = rest[1:]
    return rest


def _cp_mv_dest(args):
    """(sources, dest) or None. `-t DIR` / `--target-directory` forms are not modelled."""
    operands = []
    end_opts = False
    for a in args:
        if not end_opts and a == "--":
            end_opts = True
            continue
        if not end_opts and (a in ("-t", "--target-directory") or a.startswith("--target-directory")):
            return None
        if not end_opts and a.startswith("-") and len(a) > 1:
            continue
        operands.append(a)
    if len(operands) < 2:
        return None
    return operands[:-1], operands[-1]


def _python_writes(code):
    paths = []
    for m in _PY_OPEN_RE.finditer(code):
        if set(m.group(4)) & set("wax"):
            paths.append(m.group(2))
    for m in _PY_WRITE_TEXT_RE.finditer(code):
        paths.append(m.group(2))
    return paths


def parse_targets_detailed(cmd):
    """The pure parse, as dicts {path, lines, kind, sources}. `sources` is set only for cp/mv (so the
    resolver can map a directory destination onto dest/basename(src)). Never raises."""
    try:
        if not isinstance(cmd, str) or not cmd.strip():
            return []
        script, bodies = _split_heredocs(cmd)
        cmds = _simple_commands(_tokens(script), bodies)
    except Exception:
        return []
    found = []

    def add(path, lines, kind, sources=None):
        if isinstance(path, str) and path:
            found.append({"path": path, "lines": lines, "kind": kind, "sources": sources})

    for c in cmds:
        try:
            stdin = c.get("stdin")
            n = stdin[1] if stdin else None
            for t in c["writes"]:
                add(t, n, "redirect")
            prog, args = _program(c["words"])
            if not prog:
                continue
            if prog == "tee":
                for a in args:
                    if a == "-" or (a.startswith("-") and len(a) > 1):
                        continue
                    add(a, n, "tee")
            elif prog in ("sed", "gsed"):
                for f in _sed_files(args):
                    add(f, None, "sed-i")
            elif prog in ("cp", "mv"):
                r = _cp_mv_dest(args)
                if r:
                    add(r[1], None, prog, sources=r[0])
            elif _PY_RE.match(prog):
                code, lines = None, None
                if "-c" in args:
                    j = args.index("-c")
                    if j + 1 < len(args):
                        code = args[j + 1]
                elif (not args or args[0] == "-") and c.get("heredoc"):
                    code, lines = c["heredoc"]
                if code:
                    for p in _python_writes(code):
                        add(p, lines, "python")
        except Exception:
            continue
    return found


def parse_write_targets(cmd):
    """`[(path, est_lines)]` — every write target the command provably names, in order. est_lines
    is the feeding heredoc's body line count, or None when unknown. Never raises."""
    return [(t["path"], t["lines"]) for t in parse_targets_detailed(cmd)]


# ---- resolver (filesystem + git) ---------------------------------------------------------------

# Variables, command substitution, globs, brace expansion, or control characters (a NUL can't be a
# path at all): not a concrete path this parser can prove → allow.
_UNRESOLVABLE = re.compile(r"[$`*?\[\]{}\x00-\x1f]")


def _git(cwd, *args):
    try:
        p = subprocess.run(["git", "--literal-pathspecs", "-C", str(cwd), *args],
                           capture_output=True, text=True, timeout=5)
        return p.returncode, p.stdout
    except Exception:
        return 1, ""


def _tracked_or_new_under_tracked(cwd, rel, exists):
    """True if `rel` (relative to cwd) is tracked, or is a NEW path whose parent dir holds tracked
    files. An existing-but-untracked file (scratch) → False. Not a repo → False."""
    if exists:
        rc, out = _git(cwd, "ls-files", "--error-unmatch", "--", rel)
        return rc == 0
    parent = os.path.dirname(rel) or "."
    rc, out = _git(cwd, "ls-files", "--", parent)
    return rc == 0 and bool(out.strip())


def bash_write_targets(cmd, cwd):
    """`[(abs_path, est_lines, is_new)]` for every parsed target that resolves to a concrete path
    inside `cwd` (no variables/globs). No git or exemption filtering. Never raises."""
    out = []
    try:
        base = os.path.realpath(str(cwd))
    except Exception:
        return out
    for t in parse_targets_detailed(cmd):
        try:
            raw = t["path"]
            if _UNRESOLVABLE.search(raw) or raw.startswith("/dev/"):
                continue
            p = os.path.expanduser(raw)
            if not os.path.isabs(p):
                p = os.path.join(base, p)
            p = os.path.normpath(p)
            candidates = [p]
            if t["sources"] and os.path.isdir(p):
                candidates = [os.path.join(p, os.path.basename(s.rstrip("/")))
                              for s in t["sources"] if not _UNRESOLVABLE.search(s)]
            for cand in candidates:
                real = os.path.join(os.path.realpath(os.path.dirname(cand)),
                                    os.path.basename(cand))
                try:
                    Path(real).relative_to(base)
                except ValueError:
                    continue
                if os.path.isdir(real):
                    continue
                out.append((real, t["lines"], not os.path.exists(real)))
        except Exception:
            continue
    return out


def gated_targets(cmd, cwd, state_root, config, is_gate_exempt):
    """The subset of `bash_write_targets` the Edit gate's rule would gate, as
    `[(abs_path, est_lines, is_new)]`. `is_gate_exempt(state_root, path)` is lead_guard's
    exemption predicate (passed in so this module has no import-time dependency on it). Never
    raises; any error → []."""
    gated = []
    try:
        if not cwd:
            return gated
        base = os.path.realpath(str(cwd))
        threshold = config.get("edit_line_threshold", 40)
        new_rule = config.get("block_on_new_file", True)
        for path, lines, is_new in bash_write_targets(cmd, base):
            try:
                rel = os.path.relpath(path, base)
                if "_staging" in Path(rel).parts:
                    continue
                if is_gate_exempt(state_root, path):
                    continue
                if not _tracked_or_new_under_tracked(base, rel, not is_new):
                    continue
                if (is_new and new_rule) or (lines is not None and lines >= threshold):
                    gated.append((path, lines, is_new))
            except Exception:
                continue
    except Exception:
        return []
    return gated
