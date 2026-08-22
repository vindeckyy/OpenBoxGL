"""Centralized subprocess execution with timeout, logging, and error handling."""

import subprocess
import logging
from collections import namedtuple

logger = logging.getLogger(__name__)
SubprocessResult = namedtuple('SubprocessResult', ['returncode', 'stdout', 'stderr'])


def run_subprocess(cmd, timeout=30, **kwargs):
    """Run a subprocess with timeout, capture output, log failures."""
    try:
        result = subprocess.run(cmd, timeout=timeout, capture_output=True, text=True, check=False, **kwargs)
        return SubprocessResult(result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        logger.warning('Subprocess timed out after %ds: %s', timeout, cmd)
        return SubprocessResult(-1, '', f'Timed out after {timeout}s')
    except (OSError, ValueError) as e:
        logger.warning('Subprocess failed: %s: %s', cmd, e)
        return SubprocessResult(-1, '', str(e))
