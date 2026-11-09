"""Restore Git shell-script links in Docker contexts produced by Windows checkouts."""

import re
import stat
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent / 'docker-entrypoint.d'
    for script in root.rglob('*.sh'):
        if not script.is_symlink():
            content = script.read_text(encoding='utf-8').strip()
            # With core.symlinks=false Git checks out a link as a one-line file.
            if re.fullmatch(r'\.\./common/[A-Za-z0-9_.-]+\.sh', content):
                destination = (script.parent / content).resolve()
                if not destination.is_relative_to(root) or not destination.is_file():
                    raise RuntimeError(f'Invalid entrypoint link: {script.name}')
                script.unlink()
                script.symlink_to(content)
        destination = script.resolve()
        if not destination.is_relative_to(root):
            raise RuntimeError(f'Entrypoint target is outside its directory: {script.name}')
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


if __name__ == '__main__':
    main()
