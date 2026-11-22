# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The one-shot that recreates the Settings portal (#3058).

Runs in its OWN container, started detached by the portal through the socket
proxy, because the command it runs removes the container that asked for it: a
child process of the portal would die mid-command, between the old container's
removal and the new one's creation. The daemon owns this container, so it lives
on when the portal goes away.

Standard library only, started by path: it needs nothing of the portal's app.
The command comes from the portal (built by the compose chokepoint there). The
result goes to the volume the portal's successor reads; a failure to write it
never changes the outcome, because a successful recreate is what counts.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

RESULT_NAME = 'helper-result.json'
_TAIL = 2000


def write_result(state_dir, data):
    try:
        os.makedirs(state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=state_dir, prefix='.tmp-helper-')
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
        os.replace(tmp, os.path.join(state_dir, RESULT_NAME))
    except OSError as exc:
        print(f'could not write the result: {exc}', file=sys.stderr)


def run_once(state_dir, heal_id, grace, timeout, command):
    """Run `command` once. Returns its exit code (124 timeout, 127 not startable)."""
    # Lets the portal finish answering the request that started this.
    time.sleep(grace)
    write_result(state_dir, {'heal_id': heal_id, 'state': 'running', 'at': time.time()})
    try:
        r = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors='replace', timeout=timeout)
        code, output = r.returncode, r.stdout or ''
    except subprocess.TimeoutExpired as exc:
        code, output = 124, str(exc)
    except OSError as exc:
        code, output = 127, str(exc)
    print(output)
    write_result(state_dir, {'heal_id': heal_id, 'state': 'finished', 'exit_code': code,
                             'output': output[-_TAIL:], 'at': time.time()})
    return code


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--heal-id', required=True)
    parser.add_argument('--grace', type=float, default=2.0)
    parser.add_argument('--timeout', type=float, default=150.0)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('no command')
    return run_once(args.state_dir, args.heal_id, args.grace, args.timeout, command)


if __name__ == '__main__':
    sys.exit(main())
