// Segment a rec.sh transcript into per-command blocks.
//
// The transcript (recorded by the sandbox ForceCommand wrapper) is a flat
// string with three kinds of lines:
//   - command markers: `[<ts>] $ <command>`
//   - exit markers:    `[<ts>] (exit N)` / `[<ts>] (interactive shell)`
//   - output:          everything else
//
// Since the tmux-supervisor change, exit markers are written by the sandbox
// side even when nobody is connected, so "a block with no exit marker yet"
// has exactly one meaning: that command is still running (or the transcript
// tail was cut mid-block — only ever the FIRST block, handled as preamble).

const CMD_MARKER_RE = /^\[([^\]]+)\]\s\$\s([\s\S]*)$/;
const EXIT_MARKER_RE = /^\[([^\]]+)\]\s\((exit\s(\d+)|interactive shell)\)\s*$/;

/**
 * @returns blocks: Array<{
 *   key: string,            // stable across polls (start ts + command)
 *   ts: string | null,      // command start timestamp (null for preamble)
 *   cmd: string | null,     // command text (null for preamble)
 *   lines: string[],        // output lines (no markers)
 *   exitCode: number | null,
 *   finishedAt: string | null,
 *   interactive: boolean,   // `(interactive shell)` session
 *   preamble: boolean,      // output before the first marker (tail cut)
 * }>
 */
export function segmentTranscript(text) {
  // Commands can overlap: a long run stays open in its tmux session while the
  // agent executes other commands, so output lines and exit markers from
  // different commands interleave in the flat log. Exit markers carry no
  // command identity, so we attribute LIFO: an exit marker closes the most
  // recently opened still-open block, and output lines attach to it too. This
  // is exact for sequential commands and the best flat-log heuristic for
  // overlapping ones (raw mode always shows the verbatim transcript).
  const blocks = [];
  const open = []; // stack of still-open command blocks

  const trimTrailingBlanks = (block) => {
    while (block.lines.length && block.lines[block.lines.length - 1].trim() === '') {
      block.lines.pop();
    }
  };

  const newBlock = (fields) => {
    const block = {
      key: '',
      ts: null,
      cmd: null,
      lines: [],
      exitCode: null,
      finishedAt: null,
      interactive: false,
      preamble: false,
      ...fields,
    };
    blocks.push(block);
    return block;
  };

  for (const line of text.split('\n')) {
    const cmd = CMD_MARKER_RE.exec(line);
    if (cmd) {
      const top = open[open.length - 1];
      if (top) trimTrailingBlanks(top);
      open.push(newBlock({ key: `${cmd[1]}|${cmd[2]}`, ts: cmd[1], cmd: cmd[2] }));
      continue;
    }
    const exit = EXIT_MARKER_RE.exec(line);
    if (exit) {
      const isExit = exit[3] !== undefined;
      if (isExit) {
        const block = open.pop();
        if (block) {
          block.exitCode = parseInt(exit[3], 10);
          block.finishedAt = exit[1];
          trimTrailingBlanks(block);
        } else {
          // Exit with nothing open: tail cut mid-block, or a synthetic
          // marker for a killed session. Standalone meta block.
          newBlock({
            key: `${exit[1]}|exit`,
            ts: exit[1],
            finishedAt: exit[1],
            exitCode: parseInt(exit[3], 10),
          });
        }
      } else {
        // `(interactive shell)` opens a human session that never writes an
        // exit marker — display it, but keep it off the open stack so it
        // can't swallow another command's exit.
        newBlock({ key: `${exit[1]}|shell`, ts: exit[1], cmd: '(interactive shell)', interactive: true });
      }
      continue;
    }
    const top = open[open.length - 1];
    if (top) {
      top.lines.push(line);
      continue;
    }
    // Output with no open command: transcript tail cut (first block), or
    // detached background output in pre-tmux transcripts. Plain output block.
    const last = blocks[blocks.length - 1];
    if (last && last.preamble) {
      last.lines.push(line);
    } else {
      if (line.trim() === '') continue;
      newBlock({ key: `pre-${blocks.length}`, preamble: true, lines: [line] });
    }
  }
  for (const block of open) trimTrailingBlanks(block);
  return blocks;
}

/** Seconds elapsed from an ISO marker timestamp to `now` (ms epoch). */
export function elapsedSeconds(isoTs, nowMs) {
  const start = Date.parse(isoTs);
  if (Number.isNaN(start)) return null;
  return Math.max(0, Math.floor((nowMs - start) / 1000));
}

/** `73` -> `"1m13s"`, `7273` -> `"2h1m"`, `45` -> `"45s"`. */
export function formatElapsed(seconds) {
  if (seconds == null) return '';
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return `${m}m${s ? `${s}s` : ''}`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h${rm ? `${rm}m` : ''}`;
}
