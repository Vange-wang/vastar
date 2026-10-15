"""Discovery can read .env; its observations retain the direct-read no-persistence rule."""
import re
from pathlib import Path

from core.tools.permissions import is_env_path


def protected_result(tool, text):
    if tool != 'search_text':
        return False
    # search_text emits one absolute-path: line-number: snippet record per hit.
    return any(is_env_path(Path(match.group(1)))
               for match in re.finditer(r'^(.+?): [1-9]\d*: ', text, re.MULTILINE))
