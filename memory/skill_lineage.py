"""Locate managed verification copies of S10 bundles for the existing forget closure."""
import json


def release_copies(db, project, selected):
    references, hashes, sessions = [], set(), set()
    for row in db.execute("SELECT facts FROM tool_operations WHERE tool='skill_release'"):
        facts = json.loads(row[0])
        release = facts.get('release', {})
        if facts.get('project') == project and release.get('memory_id') in selected:
            if isinstance(release.get('hash'), str) and len(release['hash']) == 64:
                hashes.add(release['hash'])
            references.append(row[0])
    if not hashes:
        return references, sessions
    for row in db.execute("SELECT facts FROM tool_operations WHERE tool='code_change'"):
        facts = json.loads(row[0])
        owner = facts.get('owner', [])
        copies = [facts.get('hashes', {})] + [a.get('hashes', {}) for a in facts.get('attempts', [])]
        if len(owner) == 3 and owner[0] == project and any(c.get('release.json') in hashes for c in copies):
            references.append(row[0])
            sessions.add(owner[1])
    return references, sessions
