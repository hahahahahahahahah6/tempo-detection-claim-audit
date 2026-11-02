"""Audit documentation against Python syntax without executing source."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import sys

from . import __version__
from .phrases import TEMPO, NONTEMPO

GENERIC = frozenset((
    'detect_tempo', 'estimate_tempo', 'get_tempo', 'tempo_detect',
    'extract_tempo', 'track_tempo', 'tempo_track', 'detect_beats',
    'track_beats', 'beat_track', 'get_beats', 'estimate_bpm',
    'detect_bpm', 'get_bpm', 'measure_tempo', 'measure_bpm',
))


def normalize(text):
    return ' '.join(text.lower().replace('’', "'").replace("n't", ' not')
                    .replace('-', ' ').split())


def _matcher(phrases):
    ordered = sorted(set(map(normalize, phrases)), key=lambda s: (-len(s), s))
    return re.compile(r'\b(?:' + '|'.join(map(re.escape, ordered)) + r')\b')


_POSITIVE = _matcher(TEMPO)
_NEGATIVE = _matcher(NONTEMPO)


def claims(text):
    """Return affirmative and negative family flags from prose paragraphs."""
    prose = []
    fence = None
    for line in text.splitlines():
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence is not None:
            if (match and match[1][0] == fence[0]
                    and len(match[1]) >= len(fence) and not match[2].strip()):
                fence = None
                prose.append('')
        elif match:
            fence = match[1]
            prose.append('')
        else:
            prose.append(line)
    positive, negative = False, False
    for paragraph in re.split(r'\n\s*\n', '\n'.join(prose)):
        normalized = normalize(paragraph)
        negative = negative or bool(_NEGATIVE.search(normalized))
        positive = positive or bool(_POSITIVE.search(_NEGATIVE.sub(' ', normalized)))
    return positive, negative


def _module_imports(tree):
    modules, names = {}, {}

    def visit(node):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ('librosa', 'aubio', 'essentia'):
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                if alias.name != '*':
                    names[alias.asname or alias.name] = (node.module or '', alias.name)
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef, ast.Lambda)):
            for child in ast.iter_child_nodes(node):
                visit(child)

    visit(tree)
    return modules, names


def _base_name(node):
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def evidence(source):
    """Recognize a complete supported call route within one module."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    modules, names = _module_imports(tree)
    shadowed = {n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            origin, original = names.get(func.id, ('', ''))
            if (origin == 'librosa' or origin.startswith('librosa.')) and original in ('beat_track', 'tempo'):
                return True
            if original in GENERIC and original not in shadowed and func.id not in shadowed:
                return True
        elif isinstance(func, ast.Attribute):
            library = modules.get(_base_name(func.value))
            if library == 'librosa' and func.attr in ('beat_track', 'tempo'):
                return True
            if (library == 'aubio' and isinstance(func.value, ast.Name)
                    and func.attr == 'tempo'):
                return True
            if library == 'essentia' and func.attr in ('RhythmExtractor2013', 'RhythmExtractor'):
                return True
            if func.attr in GENERIC and func.attr not in shadowed:
                return True
    return False


def audit(root, ignore=()):
    """Inspect a directory and return a JSON-serializable audit result."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        raise OSError('root must be a readable, non-symlink directory')
    prefixes = [Path(p).as_posix().strip('/') for p in ignore]

    def excluded(relative):
        return any(p in ('', '.') or relative == p or relative.startswith(p + '/')
                   for p in prefixes)

    def walk_error(error):
        raise error

    positive = negative = production = testing = False
    documents, sources = [], []
    for directory, dirs, files in os.walk(str(root), followlinks=False, onerror=walk_error):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink()
                         and not excluded((base / d).relative_to(root).as_posix()))
        for filename in sorted(files):
            path = base / filename
            relative = path.relative_to(root)
            label = relative.as_posix()
            if path.is_symlink() or excluded(label):
                continue
            components = set(relative.parts[:-1])
            if label == 'README.md' or (path.suffix == '.md' and 'docs' in components):
                p, n = claims(path.read_text(encoding='utf-8'))
                positive, negative = positive or p, negative or n
                documents.append(label)
            if path.suffix != '.py':
                continue
            sources.append(label)
            if components.intersection(('fixtures', 'docs', 'examples')):
                continue
            found = evidence(path.read_text(encoding='utf-8-sig'))
            if 'tests' in components or filename.startswith('test_') or filename.endswith('_test.py'):
                testing = testing or found
            else:
                production = production or found
    findings = []
    if positive and not production:
        findings.append({
            'id': 'documented-tempo-detection-claim-false',
            'severity': 'MEDIUM' if testing else 'HIGH',
            'message': 'Supporting evidence exists only in tests.' if testing else
                       'No production evidence supports the documented claim.',
        })
    if negative and production:
        findings.append({
            'id': 'documented-no-tempo-detection-claim-false',
            'severity': 'HIGH',
            'message': 'Production evidence contradicts the documented denial.',
        })
    return dict(verdict='FAIL' if findings else 'PASS', findings=findings,
                documents=documents, sources=sources,
                production_evidence=production, test_evidence=testing)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, '%s: error: %s\n' % (self.prog, message))


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('root', help='directory to inspect')
    parser.add_argument('--json', action='store_true', help='emit structured results')
    parser.add_argument('--verbose', action='store_true', help='include inspected paths')
    parser.add_argument('--ignore', action='append', default=[], metavar='PATH',
                        help='exclude a relative path prefix; repeatable')
    parser.add_argument('--fail-on', choices=('LOW', 'MEDIUM', 'HIGH'), default='HIGH')
    parser.add_argument('--version', action='version', version=__version__)
    args = parser.parse_args(argv)
    try:
        result = audit(args.root, args.ignore)
    except (OSError, UnicodeError) as error:
        print('error: %s' % error, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(result['verdict'])
        for finding in result['findings']:
            print('{severity} {id}: {message}'.format(**finding))
        if args.verbose:
            print('Documents: %s' % ', '.join(result['documents']))
            print('Sources: %s' % ', '.join(result['sources']))
    rank = {'LOW': 0, 'MEDIUM': 1, 'HIGH': 2}
    return 2 if any(rank[f['severity']] >= rank[args.fail_on] for f in result['findings']) else 0
