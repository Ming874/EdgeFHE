"""CTest adapter: require both exit zero and an explicit functional PASS marker."""
import argparse
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--marker', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('A test command is required')
    result = subprocess.run(command, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end='', flush=True)
    if result.returncode != 0 or args.marker not in result.stdout:
        print('CPU_TEST_REJECTED exit='+str(result.returncode)+' marker_present='+str(args.marker in result.stdout))
        return 1
    print('CPU_TEST_EXIT_AND_EXPLICIT_PASS_ACCEPTED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
