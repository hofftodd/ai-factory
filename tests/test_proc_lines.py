"""Proc.lines: a process's output line by line, however long the lines are."""

from __future__ import annotations

import asyncio
import sys

from ai_factory import sandbox
from ai_factory.sandbox import Proc


async def _lines(script: str) -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    p = Proc(f"local:{proc.pid}", proc)
    out = [line async for line in p.lines()]
    assert await p.wait() == 0
    return out


async def test_a_megabyte_line_comes_through_whole():
    # A coding agent's JSON event carrying a whole file: far over readline's 64 KiB.
    out = await _lines(
        "import sys; sys.stdout.write('before\\n' + 'x' * 1_000_000 + '\\nafter\\n')"
    )
    assert out == ["before", "x" * 1_000_000, "after"]


async def test_lines_split_across_reads_and_a_last_line_without_newline():
    out = await _lines(
        "import sys, time\n"
        "sys.stdout.write('one\\ntw'); sys.stdout.flush(); time.sleep(0.05)\n"
        "sys.stdout.write('o\\n\\nthree'); sys.stdout.flush()"
    )
    assert out == ["one", "two", "", "three"]


async def test_a_line_over_the_cap_is_cut_with_a_note(monkeypatch):
    monkeypatch.setattr(sandbox, "MAX_LINE_BYTES", 1000)
    monkeypatch.setattr(sandbox, "READ_CHUNK", 300)
    out = await _lines("import sys; sys.stdout.write('y' * 5000 + '\\nnext\\n')")
    assert out == ["y" * 1000 + sandbox.cut_note(4000), "next"]


async def test_multibyte_characters_split_between_reads(monkeypatch):
    monkeypatch.setattr(sandbox, "READ_CHUNK", 7)
    out = await _lines("import sys; sys.stdout.write('héllo wörld ✓\\n')")
    assert out == ["héllo wörld ✓"]
