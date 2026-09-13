"""Unit tests for lib/bash_writes.py — the pure Bash write-target parser and its git/fs resolver
(backlog row 59)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import bash_writes as bw  # noqa: E402
import lead_guard as lg  # noqa: E402


def body(n):
    return "\n".join("line %d" % i for i in range(n))


# ---- pure parser ---------------------------------------------------------------------------------

class TestParseShapes:

    @pytest.mark.parametrize("cmd,expected", [
        ("cat > bin/relay <<EOF\n%s\nEOF" % body(60), [("bin/relay", 60)]),
        ("cat <<'EOF' > out.py\n%s\nEOF" % body(3), [("out.py", 3)]),
        ("cat >> log.txt <<EOF\na\nEOF", [("log.txt", 1)]),
        ("cat <<-END >x\n\tone\n\ttwo\n\tEND", [("x", 2)]),
        ("echo hi > f.txt", [("f.txt", None)]),
        ("echo hi >| f.txt", [("f.txt", None)]),
        ("make &> build.log", [("build.log", None)]),
        ("cat > path", [("path", None)]),
        ("echo x | tee out", [("out", None)]),
        ("echo x | tee -a a b", [("a", None), ("b", None)]),
        ("cat <<EOF | tee out\n1\n2\n3\nEOF", [("out", 3)]),
        ("cat <<EOF | sort | tee out\n1\n2\nEOF", [("out", 2)]),
        ("sed -i 's/a/b/' f.py", [("f.py", None)]),
        ("sed -i '' 's/a/b/' f.py g.py", [("f.py", None), ("g.py", None)]),
        ("sed -i.bak -e 's/a/b/' f.py", [("f.py", None)]),
        ("sed -i .bak 's/a/b/' f.py", [("f.py", None)]),
        ("sed -Ei 's/a/b/' f.py", [("f.py", None)]),
        ("sed --in-place -e s/a/b/ -e s/c/d/ f.py", [("f.py", None)]),
        ("cp a.py b.py", [("b.py", None)]),
        ("cp -r a b dest/", [("dest/", None)]),
        ("mv -f old.py new.py", [("new.py", None)]),
        ("python3 - <<PY\nopen('lib/x.py', 'w').write('hi')\nPY", [("lib/x.py", 1)]),
        ("python - <<'PY'\nimport x\nwith open(\"a.txt\", mode=\"a\") as f:\n  f.write(1)\nPY",
         [("a.txt", 3)]),
        ("python3 -c \"open('z.txt','w').write('1')\"", [("z.txt", None)]),
        ("python3 -c \"from pathlib import Path; Path('q.md').write_text('x')\"",
         [("q.md", None)]),
        ("FOO=1 sudo tee /etc/x < in", [("/etc/x", None)]),
        ("cd lib && cat > a.py <<EOF\nx\nEOF\necho done > b", [("a.py", 1), ("b", None)]),
    ])
    def test_shape(self, cmd, expected):
        assert bw.parse_write_targets(cmd) == expected

    @pytest.mark.parametrize("cmd", [
        "ls -la", "cat README.md", "git status", "grep foo <<< \"$v\"", "sed 's/a/b/' f.py",
        "python3 -c \"open('z.txt').read()\"", "python3 -c \"open('z.txt', 'r')\"",
        "python3 script.py", "cp onlyone", "cp -t dir a b", "echo 2>&1", "diff <(a) <(b)",
        "wc -l < in.txt", "", "   ", "bash -c 'echo x > y'",
    ])
    def test_no_write(self, cmd):
        assert bw.parse_write_targets(cmd) == []

    def test_heredoc_body_is_not_parsed_as_commands(self):
        cmd = "cat > a.md <<EOF\necho nope > b.txt\nsed -i s/x/y/ c\nEOF"
        assert bw.parse_write_targets(cmd) == [("a.md", 2)]

    def test_two_heredocs_consumed_in_order(self):
        cmd = "cat > a <<A\n1\nA\ncat > b <<B\n1\n2\n3\nB"
        assert bw.parse_write_targets(cmd) == [("a", 1), ("b", 3)]

    @pytest.mark.parametrize("bad", [None, 5, ["cat", ">", "x"], {"a": 1}, "echo 'unterminated > x",
                                     "cat <<EOF > x\nno terminator", "\x00\x00"])
    def test_never_raises(self, bad):
        assert isinstance(bw.parse_write_targets(bad), list)

    def test_unterminated_heredoc_still_counts_body(self):
        assert bw.parse_write_targets("cat <<EOF > x\na\nb") == [("x", 2)]


# ---- resolver ------------------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "lib").mkdir(parents=True)
    (r / "lib" / "tracked.py").write_text("x\n")
    (r / "bin").mkdir()
    (r / "bin" / "relay").write_text("x\n")
    (r / "_staging").mkdir()
    (r / "_staging" / "keep.md").write_text("x\n")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    (r / "lib" / "scratch.py").write_text("untracked\n")
    (r / "untracked_dir").mkdir()
    return r


CFG = dict(lg.LEAD_DEFAULTS)


def gated(cmd, repo, state_root, cfg=CFG):
    return [(os.path.relpath(p, os.path.realpath(repo)), n, new)
            for p, n, new in bw.gated_targets(cmd, str(repo), str(state_root), cfg,
                                              lg.is_gate_exempt)]


class TestResolver:

    def test_heredoc_over_threshold_on_tracked_file(self, repo, tmp_path):
        cmd = "cat > bin/relay <<EOF\n%s\nEOF" % body(60)
        assert gated(cmd, repo, tmp_path / "st") == [("bin/relay", 60, False)]

    def test_heredoc_under_threshold_allowed(self, repo, tmp_path):
        assert gated("cat > bin/relay <<EOF\n%s\nEOF" % body(39), repo, tmp_path / "st") == []

    def test_threshold_is_inclusive(self, repo, tmp_path):
        assert gated("cat > bin/relay <<EOF\n%s\nEOF" % body(40), repo, tmp_path / "st")

    def test_sed_on_tracked_file_unknown_count_allowed(self, repo, tmp_path):
        assert gated("sed -i s/x/y/ lib/tracked.py", repo, tmp_path / "st") == []

    def test_new_file_under_tracked_dir_gated_even_unknown_count(self, repo, tmp_path):
        assert gated("echo x > lib/new.py", repo, tmp_path / "st") == [("lib/new.py", None, True)]

    def test_new_file_rule_respects_block_on_new_file(self, repo, tmp_path):
        cfg = dict(CFG, block_on_new_file=False)
        assert gated("echo x > lib/new.py", repo, tmp_path / "st", cfg) == []

    def test_new_file_under_untracked_dir_allowed(self, repo, tmp_path):
        assert gated("echo x > untracked_dir/new.py", repo, tmp_path / "st") == []

    def test_untracked_existing_scratch_allowed(self, repo, tmp_path):
        cmd = "cat > lib/scratch.py <<EOF\n%s\nEOF" % body(80)
        assert gated(cmd, repo, tmp_path / "st") == []

    def test_outside_cwd_allowed(self, repo, tmp_path):
        assert gated("echo x > /tmp/whatever-new.txt", repo, tmp_path / "st") == []
        assert gated("echo x > ../outside.txt", repo, tmp_path / "st") == []

    def test_packet_file_exempt(self, repo, tmp_path):
        assert gated("echo x > lib/002-packet.md", repo, tmp_path / "st") == []

    def test_staging_exempt(self, repo, tmp_path):
        assert gated("echo x > _staging/new-note.md", repo, tmp_path / "st") == []

    def test_state_root_exempt(self, repo, tmp_path):
        # a state root that happens to sit inside the cwd is still exempt
        st = repo / "lib" / "state"
        assert gated("echo x > lib/state/new.json", repo, st) == []

    def test_not_a_git_repo_allowed(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        assert gated("echo x > new.py", d, tmp_path / "st") == []

    def test_cp_into_directory_resolves_basename(self, repo, tmp_path):
        (repo / "src.py").write_text("x\n")
        assert gated("cp src.py lib/", repo, tmp_path / "st") == [("lib/src.py", None, True)]

    def test_cp_onto_tracked_file_unknown_count_allowed(self, repo, tmp_path):
        assert gated("cp lib/scratch.py lib/tracked.py", repo, tmp_path / "st") == []

    def test_variables_and_globs_unresolvable_allowed(self, repo, tmp_path):
        assert gated("echo x > $OUT", repo, tmp_path / "st") == []
        assert gated("sed -i s/a/b/ lib/*.py", repo, tmp_path / "st") == []

    def test_python_heredoc_open_new_file(self, repo, tmp_path):
        cmd = "python3 - <<PY\nopen('lib/gen.py', 'w').write('x')\nPY"
        assert gated(cmd, repo, tmp_path / "st") == [("lib/gen.py", 1, True)]

    def test_dev_null_ignored(self, repo, tmp_path):
        assert gated("make > /dev/null 2>&1", repo, tmp_path / "st") == []

    def test_resolver_never_raises(self, tmp_path):
        assert bw.gated_targets("echo x > y", None, "st", CFG, lg.is_gate_exempt) == []
        assert bw.gated_targets("echo x > y", str(tmp_path), "st", {}, None) == []
