"""
Bug-hunt tests for lib/lead_guard.py — part 2 of 3: the PURE policy layer.

Model tier/ceiling, context-window choice, effort, the MCP policy, packet lint, transcript usage
and the heaviness arithmetic, auto-close, the executor agent definition, and the escalation
settings file. Part 1 (state on disk) is tests/test_lib_lead_guard_bughunt.py; part 3 (locks,
escalation, peer addressing) is tests/test_lib_lead_guard_locks_bughunt.py.

Oracle: README (the executor model/context/MCP paragraphs), skills/spawn + skills/mode, the
2026-07-12 executor-model-leak incident quoted at lib/lead_guard.py:259-266, and each function's
own docstring.

Run: pytest tests/test_lib_lead_guard_policy_bughunt.py -v
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
import lead_guard as lg  # noqa: E402


# ── model tier + ceiling (the 2026-07-12 model-leak incident) ──────────────────────────────────
class TestModelPolicy:
    @pytest.mark.parametrize("model,tier", [
        ("haiku", "haiku"), ("sonnet[1m]", "sonnet"), ("claude-opus-5", "opus"),
        ("Claude-Sonnet-5[1M]", "sonnet"), ("fable", "fable"),
        ("us.anthropic.claude-haiku-4-5-v1", "haiku"),
        ("", None), (None, None), ("gpt-4", None), ("garbage", None)])
    def test_model_tier_is_name_based_and_case_insensitive(self, model, tier):
        """model_tier: 'The tier word from TIER_ORDER contained in `model` (case-insensitive
        substring), or None if `model` is empty or names no recognized tier.'"""
        assert lg.model_tier(model) == tier

    @pytest.mark.parametrize("model,ceiling,exceeds", [
        ("haiku", "opus", False), ("sonnet", "opus", False), ("opus", "opus", False),
        ("fable", "opus", True), ("opus", "sonnet", True), ("sonnet", "haiku", True),
        ("haiku", "haiku", False)])
    def test_ceiling_comparison_follows_tier_order(self, model, ceiling, exceeds):
        assert lg.model_exceeds_ceiling(model, ceiling) is exceeds

    @pytest.mark.parametrize("model,ceiling", [
        ("some-new-model", "opus"), ("opus", "not-a-tier"), (None, "opus"), ("opus", None),
        ("", ""), ("gpt-4", "gpt-5")])
    def test_an_unrankable_model_or_ceiling_refuses_by_default(self, model, ceiling):
        """model_exceeds_ceiling: 'An unrecognized tier — for `model` OR `ceiling` — is treated as
        above-ceiling (refuse by default): a model name this list doesn't know about yet must not
        silently sail through just because it can't be ranked.' This is the incident's lesson —
        an executor's model is 'always relay's own policy decision, never an accidental
        inheritance' (lib/lead_guard.py:259-266)."""
        assert lg.model_exceeds_ceiling(model, ceiling) is True

    @pytest.mark.parametrize("model,base,suffix", [
        ("sonnet[1m]", "sonnet", "[1m]"), ("SONNET[1M]", "SONNET", "[1m]"),
        ("claude-opus-5", "claude-opus-5", ""), ("", "", ""), (None, "", "")])
    def test_split_model_suffix(self, model, base, suffix):
        assert lg.split_model_suffix(model) == (base, suffix)

    @pytest.mark.parametrize("model,is_alias", [
        ("sonnet", True), ("opus[1m]", True), ("Haiku", True), ("fable", True),
        ("claude-sonnet-5", False), ("", False), (None, False), ("sonnett", False)])
    def test_is_model_alias(self, model, is_alias):
        assert lg.is_model_alias(model) is is_alias

    def test_a_full_id_is_returned_untouched(self, tmp_path):
        """resolve_model: 'Full ids are returned untouched ("explicit")'."""
        assert lg.resolve_model("claude-opus-5[1m]", tmp_path / "m.json", "2.1.0",
                                probe=lambda a: (None, None)) == ("claude-opus-5[1m]", "explicit")

    def test_an_alias_is_probed_once_then_cached_with_its_suffix_preserved(self, tmp_path):
        """'aliases come from the cache ("cache") or the probe ("probe") … The `[1m]` suffix is
        preserved across resolution.'"""
        cache = tmp_path / "models.json"
        calls = []

        def probe(alias):
            calls.append(alias)
            return "claude-sonnet-5", None

        assert lg.resolve_model("sonnet[1m]", cache, "2.1.0", probe) == \
               ("claude-sonnet-5[1m]", "probe")
        assert lg.resolve_model("sonnet", cache, "2.1.0", probe) == ("claude-sonnet-5", "cache")
        assert calls == ["sonnet"]
        assert json.loads(cache.read_text()) == {"2.1.0": {"sonnet": "claude-sonnet-5"}}

    def test_the_cache_is_keyed_per_cli_version(self, tmp_path):
        """'the same alias resolves to different models on machines running different CLI
        versions' — so a different version must re-probe, not reuse."""
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"2.1.0": {"sonnet": "claude-sonnet-5"}}))
        assert lg.resolve_model("sonnet", cache, "9.9.9",
                                lambda a: ("claude-sonnet-6", None)) == ("claude-sonnet-6", "probe")

    def test_an_unrecognised_model_raises_rather_than_launching(self, tmp_path):
        """'an unknown model is reported as an error before any work' — ValueError, not a silent
        pass-through."""
        for err in ("unrecognized model 'x'", "the model may not exist"):
            with pytest.raises(ValueError) as e:
                lg.resolve_model("sonnet", tmp_path / "m.json", "2.1.0", lambda a: (None, err))
            assert "not recognised by this Claude Code" in str(e.value)

    def test_a_probe_failure_passes_the_alias_through_with_a_reason(self, tmp_path):
        """'Fail-open: if the probe itself fails (offline, timeout) the alias is passed through
        unchanged, with a note.'"""
        model, source = lg.resolve_model("opus", tmp_path / "m.json", "2.1.0",
                                         lambda a: (None, "timed out after 30s"))
        assert model == "opus" and source.startswith("unresolved: timed out")
        model, source = lg.resolve_model("opus", tmp_path / "m.json", "2.1.0",
                                         lambda a: (None, None))
        assert source == "unresolved: no init event"

    def test_an_unwritable_cache_does_not_break_resolution(self, tmp_path):
        """The cache write is best-effort — wrapped in its own try/except so a read-only state
        root still resolves."""
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        assert lg.resolve_model("sonnet", blocker / "sub" / "m.json", "2.1.0",
                                lambda a: ("claude-sonnet-5", None)) == \
               ("claude-sonnet-5", "probe")

    def test_a_corrupt_model_cache_reads_as_empty(self, tmp_path):
        cache = tmp_path / "m.json"
        cache.write_text("{ truncated")
        assert lg.load_model_cache(cache) == {}
        assert lg.load_model_cache(tmp_path / "nope.json") == {}


# ── context window ─────────────────────────────────────────────────────────────────────────────
class TestContextWindow:
    @pytest.mark.parametrize("raw,want", [
        ("1m", "1m"), ("1M", "1m"), ("1000k", "1m"), ("1000000", "1m"), (" 1 m ", "1m"),
        ("200k", "200k"), ("200K", "200k"), ("default", "200k"), ("standard", "200k"),
        ("normal", "200k"), (None, None), ("", None), ("500k", None), ("garbage", None)])
    def test_normalize_context_spec(self, raw, want):
        """normalize_context_spec: '"1m" | "200k" | None. Accepts 1m/1M/1000k/1000000,
        200k/200K/default/standard.'"""
        assert lg.normalize_context_spec(raw) == want

    @pytest.mark.parametrize("body,want", [
        ("CONTEXT: 1m\n", "1m"), ("- **CONTEXT**: 200k\n", "200k"),
        ("> context: 1M\n", "1m"), ("nothing here\n", None), ("CONTEXT: huge\n", None),
        ("", None), (None, None)])
    def test_packet_context_spec(self, body, want):
        assert lg.packet_context_spec(body) == want

    @pytest.mark.parametrize("model,ctx,want", [
        ("sonnet", "1m", "sonnet[1m]"), ("sonnet[1m]", "200k", "sonnet"),
        ("sonnet[1m]", None, "sonnet"), ("sonnet", None, "sonnet"),
        ("", "1m", ""), (None, "1m", None)])
    def test_model_with_context(self, model, ctx, want):
        assert lg.model_with_context(model, ctx) == want

    def test_an_explicit_1m_on_the_model_string_wins_everything(self):
        """decide_context: 'explicit `[1m]` on the model string > packet CONTEXT: line >
        reading-size heuristic > default_ctx'."""
        assert lg.decide_context("sonnet[1m]", "200k", 0) == ("sonnet[1m]", "1m", "--model")

    def test_an_explicit_1m_on_a_tier_without_one_raises(self):
        """'an EXPLICIT `[1m]` on such a tier raises ValueError (the lead asked for something
        that doesn't exist)'."""
        with pytest.raises(ValueError) as e:
            lg.decide_context("haiku[1m]", None, 0)
        assert "no 1M context window" in str(e.value)

    def test_the_packet_line_beats_the_reading_heuristic(self):
        assert lg.decide_context("sonnet", "200k", lg.CONTEXT_1M_BYTES * 10) == \
               ("sonnet", "200k", "packet CONTEXT: line")
        assert lg.decide_context("sonnet", "1m", 0) == ("sonnet[1m]", "1m", "packet CONTEXT: line")

    def test_the_reading_heuristic_fires_at_the_threshold_and_names_its_size(self):
        model, ctx, src = lg.decide_context("sonnet", None, lg.CONTEXT_1M_BYTES)
        assert (model, ctx) == ("sonnet[1m]", "1m") and "referenced reading ~" in src
        assert lg.decide_context("sonnet", None, lg.CONTEXT_1M_BYTES - 1,
                                 default_ctx="200k") == ("sonnet", "200k", "default")

    def test_a_packet_or_default_ask_for_1m_on_haiku_degrades_and_says_why(self):
        """'a packet/heuristic/default ask for 1m just degrades to 200k' — and the source string
        has to say so, since the lead reads it in `relay list`'s LAUNCH column."""
        model, ctx, src = lg.decide_context("haiku", "1m", 0)
        assert (model, ctx) == ("haiku", "200k")
        assert src == "200k (no 1M window on haiku; wanted 1m from packet CONTEXT: line)"
        model, ctx, src = lg.decide_context("haiku", None, 0, default_ctx="1m")
        assert (model, ctx) == ("haiku", "200k") and src == "200k (no 1M window on haiku)"

    def test_an_unparsable_default_falls_back_to_200k(self):
        assert lg.decide_context("sonnet", None, 0, default_ctx="wat") == \
               ("sonnet", "200k", "default")

    def test_packet_reading_bytes_counts_only_files_that_exist_once_each(self, tmp_path):
        """packet_reading_bytes: 'Total size of the files a packet refers to (paths that exist …).
        Missing paths count nothing.'"""
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "a.py").write_text("x" * 100)
        body = ("read lib/a.py and lib/a.py again, plus lib/missing.py and "
                f"{tmp_path / 'lib' / 'a.py'}\n")
        assert lg.packet_reading_bytes(body, cwd=tmp_path) == 100
        assert lg.packet_reading_bytes("", cwd=tmp_path) == 0
        assert lg.packet_reading_bytes(None) == 0

    def test_a_path_that_cannot_be_expanded_is_skipped_not_fatal(self, tmp_path):
        """packet_reading_bytes: 'paths that exist … Missing paths count nothing', and its inner
        try/except exists precisely to absorb a candidate it cannot resolve. An unknown `~user`
        prefix makes Path.expanduser raise RuntimeError, which escapes — and neither `relay spawn`
        (bin/relay:1004-1009 catches only ValueError) nor `relay lint` (bin/relay:605) catches it,
        so the whole command dies on a packet that merely MENTIONS such a path."""
        assert lg.packet_reading_bytes("see ~nosuchuser42/x.py for context", cwd=tmp_path) == 0
        assert lg.lint_packet("## Preconditions\nsee ~nosuchuser42/x.py\n" + "y" * 200,
                              cwd=tmp_path) is not None


# ── effort ─────────────────────────────────────────────────────────────────────────────────────
class TestEffort:
    @pytest.mark.parametrize("raw,want", [
        ("low", "low"), (" XHIGH ", "xhigh"), ("max", "max"),
        ("extreme", None), ("", None), (None, None)])
    def test_normalize_effort_spec(self, raw, want):
        assert lg.normalize_effort_spec(raw) == want

    @pytest.mark.parametrize("body,want", [
        ("EFFORT: high\n", "high"), ("- **EFFORT**: low\n", "low"),
        ("EFFORT: turbo\n", None), ("", None), (None, None)])
    def test_packet_effort_spec(self, body, want):
        assert lg.packet_effort_spec(body) == want


# ── MCP policy (README: executors launch with NO MCP servers unless a packet asks) ─────────────
class TestMcpPolicy:
    @pytest.mark.parametrize("raw,want", [
        (None, "none"), (True, "inherit"), (False, "none"), ("", "none"), ("none", "none"),
        ("off", "none"), ("0", "none"), ("inherit", "inherit"), ("all", "inherit"),
        ("1", "inherit"), ("linear", ["linear"]), ("b, a", ["a", "b"]),
        (["b", "a", " "], ["a", "b"]), ([], "none"), (("x",), ["x"])])
    def test_normalize_mcp_spec(self, raw, want):
        """normalize_mcp_spec: '"none" | "inherit" | sorted list of server names … None/"" →
        "none" (the policy default, never silent inheritance).'"""
        assert lg.normalize_mcp_spec(raw) == want

    @pytest.mark.parametrize("have,need,ok", [
        ("none", None, True), ("none", "none", True), ("inherit", ["x"], True),
        ("none", ["x"], False), (["x"], "inherit", False), ("inherit", "inherit", True),
        (["a", "b"], ["a"], True), (["a"], ["a", "b"], False)])
    def test_mcp_covers(self, have, need, ok):
        assert lg.mcp_covers(have, need) is ok

    @pytest.mark.parametrize("have,need,want", [
        ("none", "none", "none"), ("none", ["a"], ["a"]), (["a"], "none", ["a"]),
        (["a"], ["b"], ["a", "b"]), ("inherit", ["a"], "inherit"), (["a"], "inherit", "inherit")])
    def test_mcp_union_is_the_smallest_covering_spec(self, have, need, want):
        got = lg.mcp_union(have, need)
        assert got == want and lg.mcp_covers(got, need) and lg.mcp_covers(got, have)

    def test_mcp_spec_label(self):
        assert lg.mcp_spec_label(None) == "none"
        assert lg.mcp_spec_label(True) == "inherit"
        assert lg.mcp_spec_label(["b", "a"]) == "a,b"

    def test_packet_mcp_spec_takes_the_first_line_and_tolerates_ornament(self):
        """packet_mcp_spec: 'First such line wins' and 'a leading `-`/`*`/bold is tolerated'."""
        assert lg.packet_mcp_spec("- **MCP**: linear\nMCP: none\n") == ["linear"]
        assert lg.packet_mcp_spec("no declaration here") is None
        assert lg.packet_mcp_spec("") is None

    def test_known_servers_merge_user_project_and_dot_mcp_with_project_winning(self, tmp_path):
        """known_mcp_servers: 'Later sources win on a name clash, matching the CLI's
        project-over-user precedence. Best-effort: unreadable files contribute nothing.'"""
        cwd = tmp_path / "wt"
        cwd.mkdir()
        cj = tmp_path / ".claude.json"
        cj.write_text(json.dumps({
            "mcpServers": {"user": {"command": "u"}, "clash": {"command": "from-user"}},
            "projects": {str(cwd): {"mcpServers": {"clash": {"command": "from-project"}}}}}))
        (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"local": {"command": "l"}}}))
        got = lg.known_mcp_servers(cwd=cwd, claude_json=cj)
        assert set(got) == {"user", "clash", "local"}
        assert got["clash"]["command"] == "from-project"
        assert lg.known_mcp_servers(cwd=cwd, claude_json=tmp_path / "nope.json") == \
               {"local": {"command": "l"}}
        assert lg.known_mcp_servers(cwd=tmp_path / "nope", claude_json=tmp_path / "nope") == {}

    def test_none_produces_the_empty_strict_config_and_inherit_produces_nothing(self, tmp_path):
        """mcp_cli_flags: '"inherit" → [] … "none" → --strict-mcp-config --mcp-config
        {"mcpServers":{}}'."""
        assert lg.mcp_cli_flags("inherit") == []
        assert lg.mcp_cli_flags("none") == \
               ["--strict-mcp-config", "--mcp-config", lg.MCP_NONE_JSON]
        assert lg.mcp_cli_flags(None) == \
               ["--strict-mcp-config", "--mcp-config", lg.MCP_NONE_JSON]

    def test_an_allowlist_writes_a_per_executor_mcp_json(self, tmp_path):
        cj = tmp_path / ".claude.json"
        cj.write_text(json.dumps({"mcpServers": {"linear": {"command": "lin"},
                                                 "other": {"command": "o"}}}))
        flags = lg.mcp_cli_flags(["linear"], state_root=tmp_path / "state", exec_name="e1",
                                 claude_json=cj)
        assert flags[:2] == ["--strict-mcp-config", "--mcp-config"]
        written = json.loads(Path(flags[2]).read_text())
        assert written == {"mcpServers": {"linear": {"command": "lin"}}}

    def test_an_unknown_server_refuses_loudly_and_names_the_alternatives(self, tmp_path):
        """'a spawn must refuse loudly rather than launch an executor silently missing the tool
        the packet depends on' — and the message must point at `--mcp inherit` for plugin MCPs."""
        cj = tmp_path / ".claude.json"
        cj.write_text(json.dumps({"mcpServers": {"linear": {}}}))
        with pytest.raises(ValueError) as e:
            lg.mcp_cli_flags(["chrome"], state_root=tmp_path, exec_name="e1", claude_json=cj)
        assert "unknown MCP server(s) chrome" in str(e.value)
        assert "linear" in str(e.value) and "inherit" in str(e.value)

    def test_an_allowlist_without_somewhere_to_write_refuses(self, tmp_path):
        cj = tmp_path / ".claude.json"
        cj.write_text(json.dumps({"mcpServers": {"linear": {}}}))
        with pytest.raises(ValueError) as e:
            lg.mcp_cli_flags(["linear"], state_root=None, exec_name=None, claude_json=cj)
        assert "needs state_root and exec_name" in str(e.value)


# ── packet lint (advisory, zero-token) ─────────────────────────────────────────────────────────
LONG = "x" * 200


def codes(body, **kw):
    return {c for _lvl, c, _msg in lg.lint_packet(body, **kw)}


class TestPacketLint:
    def test_a_short_packet_and_a_missing_preconditions_section_both_warn(self):
        assert "short-packet" in codes("tiny")
        assert "no-preconditions" in codes("tiny")
        assert "no-preconditions" not in codes("## Preconditions\nall good\n" + LONG)

    def test_mcp_mentioned_but_not_declared_warns_with_the_named_tool(self):
        """'executors launch with NO MCP servers' — the packet must declare what it needs."""
        found = lg.lint_packet("## Preconditions\nuse the linear tool\n" + LONG)
        msg = [m for _l, c, m in found if c == "mcp-mentioned-not-declared"]
        assert msg and "linear" in msg[0]
        assert "mcp-mentioned-not-declared" not in codes(
            "## Preconditions\nMCP: linear\nuse the linear tool\n" + LONG)

    def test_a_configured_server_name_is_also_detected(self):
        assert "mcp-mentioned-not-declared" in codes(
            "## Preconditions\ntalk to sentry-mcp\n" + LONG, known_servers={"sentry-mcp"})

    def test_an_mcp_name_no_config_file_defines_warns_before_spawn_refuses(self):
        assert "mcp-unknown-server" in codes(
            "## Preconditions\nMCP: ghost\n" + LONG, known_servers={"linear"})
        assert "mcp-unknown-server" not in codes(
            "## Preconditions\nMCP: linear\n" + LONG, known_servers={"linear"})

    def test_an_unparsable_mcp_value_is_reported_as_unparsable(self):
        """The rule's own message is the contract (lib/lead_guard.py:1937-1939): 'MCP: line
        present but its value isn't none/inherit/a,b'. Today such a value is silently read as a
        SERVER NAME, so the lead either sees nothing or sees the misleading
        'not configured in ~/.claude.json' instead."""
        assert "mcp-unparsable" in codes("## Preconditions\nMCP: whatever you think best\n" + LONG)

    def test_an_unparsable_context_or_effort_line_warns(self):
        assert "context-unparsable" in codes("## Preconditions\nCONTEXT: enormous\n" + LONG)
        assert "effort-unparsable" in codes("## Preconditions\nEFFORT: turbo\n" + LONG)

    def test_a_big_reading_packet_pinned_to_200k_warns_and_unpinned_informs(self, tmp_path):
        big = lg.CONTEXT_1M_BYTES + 1
        assert "context-200k-big-reading" in codes(
            "## Preconditions\nCONTEXT: 200k\n" + LONG, reading_bytes=big)
        assert "context-auto-1m" in codes("## Preconditions\n" + LONG, reading_bytes=big)
        assert "reading-front-loaded" in codes(
            "## Preconditions\n" + LONG, reading_bytes=2 * lg.CONTEXT_1M_BYTES)

    def test_1m_asked_of_haiku_warns(self):
        assert "context-1m-on-haiku" in codes(
            "## Preconditions\nCONTEXT: 1m\n" + LONG, model="haiku")
        assert "context-1m-on-haiku" not in codes(
            "## Preconditions\nCONTEXT: 1m\n" + LONG, model="sonnet")

    def test_role_violations_the_executor_cannot_honour_are_warned(self):
        """GATES: executors stage only (commit/push are denied) and never ask in the tab."""
        assert "asks-to-commit" in codes("## Preconditions\ncommit the fix\n" + LONG)
        assert "asks-to-commit" in codes("## Preconditions\nrun git push\n" + LONG)
        assert "asks-to-ask" in codes("## Preconditions\nask the user which one\n" + LONG)
        assert "asks-to-ask" in codes("## Preconditions\nconfirm with the lead first\n" + LONG)

    def test_packet_shape_nudges_follow_the_spawn_rubric(self, tmp_path):
        haiku_shaped = ("## Preconditions\nready\n## Acceptance\n```\npytest tests/ -q\n```\n"
                        "Files: `lib/a.py`, `lib/b.py`\n" + LONG)
        assert "shape-haiku" in codes(haiku_shaped, model="sonnet")
        assert "shape-haiku" not in codes(haiku_shaped, model="haiku")
        assert "shape-haiku" not in codes(haiku_shaped.replace("ready", "investigate why"),
                                          model="sonnet")
        opus_shaped = "## Preconditions\nready\nfigure out why the wake is lost\n" + LONG
        assert "shape-opus" in codes(opus_shaped, model="sonnet")
        assert "shape-opus" not in codes(opus_shaped, model="opus")
        assert "shape-opus" not in codes(opus_shaped + "\nRepro: run x\n", model="sonnet")

    def test_a_clean_packet_lints_clean(self, tmp_path):
        body = ("Do the thing.\n\n## Preconditions\nthe checkout is pulled\n\n"
                "MCP: none\nCONTEXT: 200k\nEFFORT: high\n\n" + LONG)
        assert codes(body, cwd=tmp_path, known_servers=set(), model="sonnet") == set()


# ── transcript usage + heaviness ───────────────────────────────────────────────────────────────
def transcript(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines) + "\n")
    return p


def assistant(mid, prompt_in=0, cache_read=0, cache_create=0, out=0, model="claude-sonnet-5",
              ts="2026-09-02T03:55:36.209Z"):
    return {"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "model": model, "usage": {
            "input_tokens": prompt_in, "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create, "output_tokens": out}}}


class TestTranscriptUsage:
    def test_repeated_message_ids_are_deduplicated(self, tmp_path):
        """'Assistant lines are repeated once per content block with the same message id and
        usage — dedup by message id (last wins) or every multi-block turn is counted N times.'"""
        p = transcript(tmp_path, [assistant("m1", 10, 100, 5, 20),
                                  assistant("m1", 10, 100, 5, 20),
                                  assistant("m2", 1, 2, 3, 4)])
        u = lg.transcript_usage(p)
        assert u["requests"] == 2
        assert u["prompt"] == (10 + 100 + 5) + (1 + 2 + 3)
        assert u["output"] == 24
        assert u["models"] == {"claude-sonnet-5": 2}

    def test_last_prompt_is_the_last_request_not_a_sum(self, tmp_path):
        """'`last_prompt`/`last_ts` are the LAST request's reading, not a sum — that's the LIVE
        context.'"""
        p = transcript(tmp_path, [assistant("m1", 1000, 0, 0, 1),
                                  assistant("m2", 7, 3, 2, 1)])
        u = lg.transcript_usage(p)
        assert u["last_prompt"] == 12 and u["last_ts"] is not None

    def test_non_assistant_lines_empty_usage_and_broken_json_are_skipped(self, tmp_path):
        p = transcript(tmp_path, [
            {"type": "user", "message": {"usage": {"input_tokens": 9999}}},
            '{"usage": broken json',
            {"type": "assistant", "message": {"id": "x", "usage": {}}},
            {"type": "assistant", "message": {"id": "y"}},
            "a line with no usage key at all",
            assistant("m1", 5, 0, 0, 1)])
        u = lg.transcript_usage(p)
        assert u["requests"] == 1 and u["prompt"] == 5

    def test_the_cache_hit_rate_is_read_over_the_whole_session(self, tmp_path):
        p = transcript(tmp_path, [assistant("m1", 10, 90, 0, 1)])
        assert lg.transcript_usage(p)["cache_hit_rate"] == 0.9
        p2 = transcript(tmp_path, [{"type": "assistant", "message": {"id": "z", "usage": {
            "input_tokens": 0, "output_tokens": 3}}}])
        assert lg.transcript_usage(p2)["cache_hit_rate"] is None

    def test_an_unreadable_transcript_is_none_not_an_exception(self, tmp_path):
        """'or None when unreadable' — None is what makes is_heavy fall back to the MB proxy."""
        assert lg.transcript_usage(tmp_path / "nope.jsonl") is None
        d = tmp_path / "adir"
        d.mkdir()
        assert lg.transcript_usage(d) is None

    def test_a_timestamp_that_will_not_parse_leaves_last_ts_none(self, tmp_path):
        p = transcript(tmp_path, [assistant("m1", 1, 0, 0, 1, ts="not-a-timestamp")])
        assert lg.transcript_usage(p)["last_ts"] is None
        assert lg._parse_ts(None) is None and lg._parse_ts("nope") is None

    def test_cache_state_reads_warm_cold_and_nothing(self):
        """cache_state: warm inside the TTL, else ('cold', est_rewrite_tokens); None when there is
        nothing to read."""
        u = {"last_ts": 1000.0, "last_prompt": 212_000}
        assert lg.cache_state(u, 1000 + 59 * 60, 60) == ("warm", None)
        assert lg.cache_state(u, 1000 + 61 * 60, 60) == ("cold", 212_000)
        assert lg.cache_state(None, 0, 60) is None
        assert lg.cache_state({"last_prompt": 5}, 0, 60) is None
        assert lg.cache_state({"last_ts": 1.0}, 10 ** 9, 60) == ("cold", 0)

    @pytest.mark.parametrize("n,want", [
        (0, "0"), (999, "999"), (1234, "1.2k"), (9999, "10.0k"), (10_000, "10k"),
        (1_234_567, "1.2M"), ("nope", "-"), (None, "-")])
    def test_human_tokens(self, n, want):
        """human_tokens: '1234 → "1.2k", 1_234_567 → "1.2M", 0 → "0"'."""
        assert lg.human_tokens(n) == want

    def test_usage_cell_renders_the_optional_cache_segment(self):
        u = {"prompt": 1_200_000, "output": 34_000}
        assert lg.usage_cell(None) == "-"
        assert lg.usage_cell(u) == "1.2M/34k"
        assert lg.usage_cell(u, ("warm", None)) == "1.2M/34k ·warm"
        assert lg.usage_cell(u, ("cold", 212_000)) == "1.2M/34k ·cold~212k"
        assert lg.usage_cell(u, ("weird", 1)) == "1.2M/34k"   # unrecognised state → no segment

    def test_heaviness_prefers_real_usage_and_falls_back_to_mb(self):
        """is_heavy: 'usage is None means the transcript couldn't be parsed for real usage at
        all … falls back to the raw MB reading … Both unavailable → never heavy.'"""
        assert lg.is_heavy({"last_prompt": 150_000}, None, 150_000, 5) is True
        assert lg.is_heavy({"last_prompt": 149_999}, 99.0, 150_000, 5) is False  # MB ignored
        assert lg.is_heavy(None, 6.0, 150_000, 5) is True
        assert lg.is_heavy(None, 1.0, 150_000, 5) is False
        assert lg.is_heavy(None, None, 150_000, 5) is False
        assert lg.is_heavy({}, None, 150_000, 5) is False

    def test_the_heaviness_text_names_which_signal_it_used(self):
        assert lg.heavy_reading_text({"last_prompt": 212_000}, 9.0) == "212k ctx"
        assert lg.heavy_reading_text(None, 9.0) == \
               "9.0MB (transcript unreadable for usage; MB proxy)"
        assert lg.heavy_reading_text(None, None) == "unknown"

    def test_launch_cell_reads_mcp_context_and_role(self):
        """launch_cell: "'none/1m/A/high' (A = agent-roled, G = legacy full-GATES packets; '?' for
        records that predate a field)" — README "Executor effort", last sentence: "Shown in
        `relay list`'s LAUNCH column as an always-present fourth segment (`none/1m/A/high`)" — so
        a bare record with no fields at all renders all four segments as '?'."""
        assert lg.launch_cell({}) == "?/?/?/?"
        assert lg.launch_cell({"mcp": "none", "context": "200k", "agent": True}) == "none/200k/A/?"
        assert lg.launch_cell({"mcp": ["a"], "model": "sonnet[1m]", "agent": False}) == "a/1m/G/?"
        assert lg.launch_cell({"mcp": "none", "context": "1m", "agent": True,
                               "effort": "high"}) == "none/1m/A/high"


# ── auto-close policy ──────────────────────────────────────────────────────────────────────────
def sess(**over):
    s = {"session_id": "e1", "status": "reported"}
    s.update(over)
    return s


def decide(**over):
    kw = dict(report_age=10_000, surfaced=True, queued=0, claimed=["a.py"], dirty=[],
              heavy=False, idle_minutes=60)
    kw.update(over)
    return lg.auto_close_decision(sess(**kw.pop("session", {})), **kw)


class TestAutoClose:
    def test_landed_work_is_closed_and_a_heavy_session_is_retired(self):
        """'landed — the report's claimed files are clean in the worktree … Both REQUIRE that the
        owning lead has already surfaced the report.'"""
        assert decide() == ("close", "landed")
        assert decide(heavy=True) == ("retire", "landed")

    def test_the_landed_path_waits_out_the_grace_window(self):
        """'Immediate (after a short grace so a report written seconds ago isn't judged
        mid-stage).'"""
        assert decide(report_age=lg.AUTO_CLOSE_LANDED_GRACE_SECONDS - 1, idle_minutes=0) is None
        assert decide(report_age=lg.AUTO_CLOSE_LANDED_GRACE_SECONDS, idle_minutes=0) == \
               ("close", "landed")

    def test_dirty_claimed_files_mean_the_work_has_not_landed(self):
        assert decide(dirty=["a.py"], idle_minutes=0) is None
        assert decide(dirty=["b.py"]) == ("close", "landed")

    def test_the_idle_timer_is_the_fallback_and_names_the_age(self):
        got = decide(claimed=[], report_age=3 * 3600)
        assert got == ("close", "idle 180m")
        assert decide(claimed=[], report_age=59 * 60) is None
        assert decide(claimed=[], report_age=10 ** 6, idle_minutes=0) is None

    @pytest.mark.parametrize("over", [
        dict(session={"keep": True}), dict(session={"status": "busy"}),
        dict(session={"status": "closed"}), dict(surfaced=False), dict(queued=2),
        dict(report_age=None)])
    def test_never_parks_pinned_busy_unsurfaced_or_queued_sessions(self, over):
        """'never park a report nobody has looked at — and never touch busy/stalled/queued/pinned
        sessions.'"""
        assert decide(**over) is None

    def test_an_empty_session_record_is_never_parked(self):
        assert lg.auto_close_decision(None, report_age=1, surfaced=True, queued=0, claimed=[],
                                      dirty=[], heavy=False, idle_minutes=60) is None


# ── the executor agent definition + escalation settings ────────────────────────────────────────
class TestExecutorAgent:
    def test_the_shipped_agent_file_parses_into_a_usable_definition(self):
        """load_executor_agent feeds `--agents <json> --agent relay-executor`; the shipped
        agents/executor.md must therefore have a body and its disallowedTools."""
        agents = lg.load_executor_agent(REPO_ROOT)
        assert set(agents) == {lg.EXECUTOR_AGENT_NAME}
        a = agents[lg.EXECUTOR_AGENT_NAME]
        assert a["disallowedTools"] == ["Agent"]
        assert "STAGE, NEVER COMMIT" in a["prompt"] and a["description"]

    def test_the_agent_prompt_keeps_its_context_hygiene_section(self):
        """The CONTEXT HYGIENE rules keep executors from filling their window with raw tool
        output; they must not be silently dropped from the shipped prompt."""
        prompt = lg.load_executor_agent(REPO_ROOT)[lg.EXECUTOR_AGENT_NAME]["prompt"]
        assert "CONTEXT HYGIENE" in prompt
        for phrase in ("line range", "grep", "screenshot"):
            assert phrase in prompt
        assert prompt.index("GATES") < prompt.index("CONTEXT HYGIENE") < prompt.index("REPORT FORMAT")

    def test_the_launch_flags_keep_the_git_denies_as_one_argument(self, tmp_path):
        """'The denies are ONE comma-joined argument on purpose: `--disallowedTools` is variadic
        and would otherwise swallow the prompt positional that follows it.'"""
        flags = lg.executor_agent_flags(REPO_ROOT)
        assert flags[0] == "--agents" and json.loads(flags[1])
        assert flags[2:4] == ["--agent", lg.EXECUTOR_AGENT_NAME]
        assert flags[4] == "--disallowedTools"
        assert flags[5] == "Bash(git commit*),Bash(git push*)"
        assert len(flags) == 6

    def test_a_missing_or_bodyless_agent_file_degrades_to_no_flags(self, tmp_path):
        """'or None if the plugin has no agents/executor.md (a spawn then falls back to the full
        GATES footer in the packet)'."""
        assert lg.load_executor_agent(tmp_path / "nope") is None
        assert lg.executor_agent_flags(tmp_path / "nope") == []
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "executor.md").write_text("---\nname: x\n---\n\n")
        assert lg.load_executor_agent(tmp_path) is None

    def test_front_matter_parsing_splits_tool_lists_and_keeps_the_body(self):
        """parse_agent_file: '`tools`/`disallowedTools` values are split on commas into lists.'"""
        fields, body = lg.parse_agent_file(
            "---\nname: x\ntools: Read, Bash , \ndisallowedTools: Agent\n"
            " indented: ignored\n---\nbody text\n")
        assert fields["tools"] == ["Read", "Bash"]
        assert fields["disallowedTools"] == ["Agent"]
        assert "indented" not in fields
        assert body == "body text"
        assert lg.parse_agent_file("no front matter") == ({}, "no front matter")

    def test_a_tools_allowlist_reaches_the_agent_definition(self, tmp_path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "executor.md").write_text(
            "---\ndescription: d\ntools: Read, Bash\n---\nprompt body\n")
        a = lg.load_executor_agent(tmp_path)[lg.EXECUTOR_AGENT_NAME]
        assert a["tools"] == ["Read", "Bash"] and a["prompt"] == "prompt body"

    def test_the_escalation_settings_pass_the_relay_name_as_an_argument(self, tmp_path):
        """build_escalation_settings: '`exec_name` is passed to the hook AS AN ARGUMENT because
        the hook cannot otherwise learn which executor it is … Found live: the push never fired in
        production until the name was passed explicitly.'"""
        s = lg.build_escalation_settings("/plug", "bh-lib", timeout=45)
        hook = s["hooks"]["Stop"][0]["hooks"][0]
        assert hook["command"] == "/plug/hooks/executor_escalation.py bh-lib"
        assert hook["timeout"] == 45 and hook["type"] == "command"
        assert "asyncRewake" not in json.dumps(s)

    def test_the_settings_file_is_per_executor_and_can_carry_a_fallback_model(self, tmp_path):
        """Packet-002 item 5 (lead-found): a bare string here made Claude Code 2.1.263 refuse the
        settings file — `fallbackModel` must be a list — so `write_escalation_settings` now wraps
        it (see `lead_guard.normalize_fallback_models`)."""
        p = lg.write_escalation_settings(tmp_path, "/plug", "bh-lib", fallback="sonnet")
        assert Path(p) == tmp_path / "bh-lib" / "settings.json"
        content = json.loads(Path(p).read_text())
        assert content["fallbackModel"] == ["sonnet"]
        assert content["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("bh-lib")
        p2 = lg.write_escalation_settings(tmp_path, "/plug", "bh-lib", include_hooks=False)
        assert json.loads(Path(p2).read_text()) == {}

    def test_a_write_failure_falls_back_to_spawning_without_escalation(self, tmp_path):
        """'Returns the path (str), or None on any failure — a write failure must fall back to
        spawning WITHOUT escalation armed rather than failing the whole spawn.'"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        assert lg.write_escalation_settings(blocker, "/plug", "e1") is None
