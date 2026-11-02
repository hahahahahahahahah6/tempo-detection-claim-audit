import ast
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import zipfile
import unittest

from tempo_detection_claim_audit import __version__, phrases
from tempo_detection_claim_audit.audit import audit, claims, evidence, main, normalize

EXPECTED_TEMPO = ('tempo detection', 'detects tempo', 'detect tempo', 'tempo estimation', 'estimates tempo', 'estimate tempo', 'beat tracking', 'tracks beats', 'track beats', 'beat detection', 'detects beats', 'detect beats', 'bpm detection', 'detects bpm', 'bpm estimation', 'estimates bpm', 'tempo analysis', 'analyses tempo', 'analyzes tempo', 'tempo tracking', 'tracks tempo', 'beat tracker', 'tempo detector', 'rhythm detection', 'detects rhythm', 'musical tempo detection', 'detects musical tempo', 'automatic tempo detection', 'real-time tempo detection', 'tempo measurement', 'measures tempo', 'beat estimation', 'estimates beats', 'downbeat detection', 'detects downbeats', 'tempo identification', 'identifies tempo', 'bpm measurement', 'measures bpm', 'tempo recognition', 'recognizes tempo', 'rhythm tracking', 'tracks rhythm', 'beat tracking algorithm', 'tempo detection algorithm', 'supports tempo detection', 'supports beat tracking', 'tempo estimator', 'bpm estimator')
EXPECTED_NONTEMPO = ('no tempo detection', 'does not detect tempo', 'without tempo detection', 'no tempo estimation', 'does not estimate tempo', 'without tempo estimation', 'no beat tracking', 'does not track beats', 'tempo detection not supported', 'does not support tempo detection', 'cannot detect tempo', 'never detects tempo', 'no beat detection', 'does not detect beats', 'no bpm estimation', 'does not estimate bpm', 'no rhythm detection', 'does not detect rhythm', 'without tempo analysis', 'no tempo detector', 'tempo detection unavailable', 'does not measure tempo', 'no beat tracker', 'does not track rhythm')

GENERIC_NAMES = (
    'detect_tempo', 'estimate_tempo', 'get_tempo', 'tempo_detect',
    'extract_tempo', 'track_tempo', 'tempo_track', 'detect_beats',
    'track_beats', 'beat_track', 'get_beats', 'estimate_bpm',
    'detect_bpm', 'get_bpm', 'measure_tempo', 'measure_bpm',
)
GOOD = {
    'librosa_attribute': 'import librosa\nlibrosa.beat_track(y)',
    'librosa_tempo': 'import librosa\nlibrosa.tempo(y)',
    'librosa_beat_chain': 'import librosa\nlibrosa.beat.beat_track(y)',
    'librosa_tempo_chain': 'import librosa\nlibrosa.feature.rhythm.tempo(y)',
    'librosa_alias': 'import librosa as l\nl.beat.tempo(y)',
    'librosa_from': 'from librosa import beat_track\nbeat_track(y)',
    'librosa_from_alias': 'from librosa.beat import tempo as t\nt(y)',
    'librosa_shadow_exception': 'import librosa\ndef beat_track(): pass\nlibrosa.beat_track(y)',
    'aubio': 'import aubio\naubio.tempo()',
    'aubio_alias': 'import aubio as a\na.tempo()',
    'aubio_shadow_exception': 'import aubio\ndef tempo(): pass\naubio.tempo()',
    'essentia': 'import essentia\nessentia.RhythmExtractor2013()',
    'essentia_chain': 'import essentia as e\ne.standard.RhythmExtractor()',
    'essentia_shadow_exception': 'import essentia\ndef RhythmExtractor(): pass\nessentia.RhythmExtractor()',
}
BAD = {
    'bare_imports': 'import librosa, aubio, essentia',
    'load': 'import librosa\nlibrosa.load(path)',
    'soundfile': 'import soundfile\nsoundfile.read(path)',
    'wave': 'import wave\nwave.open(path).readframes(100)',
    'torchaudio': 'import torchaudio\ntorchaudio.load(path)',
    'capture': 'mic.listen(); mic.record(); sounddevice.rec(); stream.read()',
    'playback': 'audio.play(); sounddevice.play(y)',
    'speech': 'model.transcribe(y); recognizer.recognize_google(y); engine.stt(y)',
    'tts': 'engine.say(text); engine.synthesize(text)',
    'vad': 'vad.is_speech(y); model.get_speech_timestamps(y); Pipeline.from_pretrained("segmentation")',
    'keyword': 'pvporcupine.create(); Model().predict(y); engine.detect_keyword(y)',
    'verification': 'model.verify_files(a,b); encoder.embed_utterance(a); engine.verify_speaker(a,b)',
    'diarization': 'Pipeline.from_pretrained("pyannote/speaker-diarization"); engine.diarize(y)',
    'noise': 'noisereduce.reduce_noise(y); df.enhance.enhance(y); engine.suppress_noise(y); engine.remove_noise(y)',
    'fingerprinting': 'acoustid.fingerprint_file(path); Dejavu().recognize(path); engine.fingerprint_audio(y)',
    'events': 'AudioTagging().inference(y); hub.load("yamnet")(y); engine.detect_sound_event(y)',
    'genre': 'classifier.predict(y); pipeline("audio-classification")(y)',
    'separation': 'demucs.separate(y); Separator().separate_to_file(path); engine.separate_stems(y)',
    'features': 'import librosa\nlibrosa.feature.chroma_stft(y); librosa.feature.mfcc(y); librosa.feature.spectral_centroid(y); librosa.feature.zero_crossing_rate(y)',
    'pitch': 'import librosa, aubio\nlibrosa.piptrack(y); librosa.yin(y); crepe.predict(y); aubio.pitch(); sound.to_pitch(); engine.detect_pitch(y)',
    'onsets': 'import librosa\nlibrosa.onset.onset_detect(y); librosa.onset.onset_strength(y); engine.detect_onsets(y)',
    'hashing': 'hashlib.md5(data); hashlib.sha256(data)',
    'filter': 'signal.lfilter(b,a,y); equalizer.filter(y)',
    'mix': 'engine.mix_tracks(a,b)',
    'normalize': 'librosa.util.normalize(y); engine.normalize_volume(y)',
    'language': 'engine.detect_language(text)',
    'translation': 'engine.translate(text)',
    'lone_other_library': 'import madmom',
    'wrong_tempo': 'other.tempo(); other.RhythmExtractor2013(); other.RhythmExtractor()',
    'unbound_tempo': 'librosa.tempo(); aubio.tempo(); essentia.standard.RhythmExtractor2013()',
    'aubio_chain': 'import aubio\naubio.other.tempo()',
    'wrong_import': 'import other as librosa\nlibrosa.tempo()',
    'subprocess': 'subprocess.run(["tempo", "input.wav"])',
    'string': 'text = "librosa.beat.beat_track(audio)"',
    'reference': 'import librosa\nlibrosa.beat.beat_track',
    'bare_name': 'detect_tempo(y); beat_track(y)',
    'invalid': 'import librosa\nlibrosa.tempo((',
    'nested_import': 'def setup():\n import librosa\nlibrosa.tempo(y)',
    'relative_import': 'from .librosa import tempo\ntempo(y)',
    'shadow_function': 'from engine import detect_tempo\ndef detect_tempo(y): pass\ndetect_tempo(y)',
    'shadow_method': 'class Stub:\n def detect_tempo(self,y): pass\nengine.detect_tempo(y)',
    'shadow_alias': 'from engine import estimate_bpm as get\ndef get(y): pass\nget(y)',
    'shadow_async': 'async def detect_beats(y): pass\nengine.detect_beats(y)',
}


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(self.root)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_phrase_oracle(self):
        tree = ast.parse(Path(phrases.__file__).read_text(encoding='utf-8'))
        values = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign)}
        self.assertEqual(tuple(map(normalize, values['TEMPO'])), tuple(map(normalize, EXPECTED_TEMPO)))
        self.assertEqual(tuple(map(normalize, values['NONTEMPO'])), tuple(map(normalize, EXPECTED_NONTEMPO)))
        self.assertEqual(len(values['TEMPO']), 49)
        self.assertEqual(len(values['NONTEMPO']), 24)

    def test_good(self):
        self.write('README.md', 'Supports tempo detection.')
        self.write('app.py', GOOD['librosa_beat_chain'])
        self.assertEqual(self.run_cli()[0:2], (0, 'PASS\n'))

    def test_bad_exact_and_json(self):
        self.write('README.md', 'Tempo detection')
        self.write('app.py', 'import librosa')
        code, out, _ = self.run_cli('--json')
        data = json.loads(out)
        self.assertEqual(code, 2)
        self.assertEqual(data['verdict'], 'FAIL')
        self.assertEqual(len(data['findings']), 1)
        self.assertEqual(data['findings'][0]['id'], 'documented-tempo-detection-claim-false')
        self.assertEqual(data['findings'][0]['severity'], 'HIGH')
        self.assertTrue(data['findings'][0]['message'])

    def test_test_only(self):
        self.write('README.md', 'Detects tempo')
        self.write('tests/helper.py', GOOD['aubio'])
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.run_cli('--fail-on', 'MEDIUM')[0], 2)
        finding = audit(self.root)['findings'][0]
        self.assertEqual(finding['id'], 'documented-tempo-detection-claim-false')
        self.assertEqual(finding['severity'], 'MEDIUM')

    def test_denial_exact(self):
        self.write('README.md', 'Does not detect tempo')
        self.write('app.py', GOOD['essentia'])
        code, out, _ = self.run_cli('--json')
        result = json.loads(out)
        self.assertEqual(code, 2)
        self.assertEqual(result['findings'][0]['id'], 'documented-no-tempo-detection-claim-false')
        self.assertEqual(result['findings'][0]['severity'], 'HIGH')
        self.assertEqual(len(result['findings']), 1)

    def test_no_claim(self):
        self.write('README.md', 'An audio utility.')
        self.write('app.py', GOOD['aubio'])
        self.assertEqual(audit(self.root)['verdict'], 'PASS')

    def test_no_source_asymmetry(self):
        self.write('README.md', 'Tempo detection')
        self.assertEqual(self.run_cli()[0], 2)
        self.write('README.md', 'No tempo detection')
        self.assertEqual(self.run_cli()[0], 0)

    def test_both_families_once(self):
        self.write('README.md', 'Detects tempo.\n\nNo tempo detection.')
        result = audit(self.root)
        self.assertEqual(len(result['findings']), 1)
        self.write('app.py', GOOD['aubio'])
        self.assertEqual(len(audit(self.root)['findings']), 1)

    def test_fences(self):
        for marker in ('```', '~~~', '   ````'):
            with self.subTest(marker=marker):
                self.assertEqual(claims(marker + 'python\nTempo detection\n' + marker), (False, False))
        self.assertEqual(claims('```\nTempo detection\n~~~\n'), (False, False))
        self.assertEqual(claims('```\nclaim\n```\nTempo detection'), (True, False))
        self.assertEqual(claims('    ```\nTempo detection'), (True, False))

    def test_inline(self):
        self.assertEqual(claims('Supports `tempo detection`.'), (True, False))

    def test_normalization_and_boundaries(self):
        self.assertEqual(claims("DOESN'T detect-tempo"), (False, True))
        self.assertEqual(claims('Real-time   TEMPO\n detection'), (True, False))
        self.assertEqual(claims('detects tempos'), (False, False))
        self.assertEqual(claims('tempo\n\ndetection'), (False, False))
        self.assertEqual(claims('No tempo detection. Also detects beats.'), (True, True))
        self.assertEqual(claims('tempo detection algorithm'), (True, False))

    def test_document_selection(self):
        self.write('other.md', 'Tempo detection')
        self.assertEqual(audit(self.root)['verdict'], 'PASS')
        self.write('nested/docs/more/deep.md', 'Detects tempo')
        self.assertEqual(self.run_cli()[0], 2)

    def test_excluded_components(self):
        self.write('README.md', 'Tempo detection')
        for directory in ('fixtures', 'docs', 'examples'):
            self.write(directory + '/impl.py', GOOD['aubio'])
        self.assertFalse(audit(self.root)['production_evidence'])
        self.assertFalse(audit(self.root)['test_evidence'])

    def test_test_filename_patterns(self):
        self.write('README.md', 'Tempo detection')
        for filename in ('test_impl.py', 'impl_test.py', 'nested/tests/helper.py'):
            path = self.write(filename, GOOD['aubio'])
            result = audit(self.root)
            self.assertTrue(result['test_evidence'])
            self.assertFalse(result['production_evidence'])
            path.unlink()

    def test_symlinks(self):
        self.write('README.md', 'Tempo detection')
        excluded = self.write('fixtures/real.py', GOOD['aubio'])
        (self.root / 'linked.py').symlink_to(excluded)
        (self.root / 'linked_dir').symlink_to(excluded.parent, target_is_directory=True)
        self.assertEqual(self.run_cli()[0], 2)
        self.write('README.md', 'Unrelated prose')
        doc = self.write('fixtures/claim.txt', 'Tempo detection')
        (self.root / 'docs').mkdir()
        (self.root / 'docs/linked.md').symlink_to(doc)
        self.assertEqual(self.run_cli()[0], 0)

    def test_single_module_scope(self):
        self.write('README.md', 'Tempo detection')
        self.write('imports.py', 'import librosa, aubio, essentia\nfrom engine import detect_tempo')
        self.write('calls.py', 'librosa.tempo(y); aubio.tempo(); essentia.RhythmExtractor(); detect_tempo(y)')
        self.assertEqual(self.run_cli()[0], 2)

    def test_ignore_repeatable_prefix(self):
        self.write('README.md', 'Tempo detection')
        self.write('one/app.py', GOOD['aubio'])
        self.write('two/app.py', GOOD['aubio'])
        self.assertEqual(self.run_cli('--ignore', 'one', '--ignore', 'two')[0], 2)
        self.assertEqual(self.run_cli('--ignore', 'on')[0], 0)
        self.assertEqual(self.run_cli('--ignore', 'README.md')[0], 0)

    def test_verbose(self):
        self.write('README.md', 'Utility')
        self.assertIn('README.md', self.run_cli('--verbose')[1])

    def test_usage_errors(self):
        for args in ([], ['--unknown'], ['--fail-on', 'INVALID', str(self.root)]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(args)
                self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.run_cli('--ignore', 'x')[0], 0)

    def test_io_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.root / 'missing')]), 1)
        self.write('README.md', '\udcff'.encode('utf-8', errors='surrogateescape').decode('latin1'))
        (self.root / 'README.md').write_bytes(b'\xff')
        self.assertEqual(self.run_cli()[0], 1)

    def test_version_and_module_entry(self):
        result = subprocess.run([sys.executable, '-m', 'tempo_detection_claim_audit', '--version'],
                                capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, '0.1.0'))
        self.assertEqual(__version__, '0.1.0')
        self.write('README.md', 'Tempo detection')
        result = subprocess.run([sys.executable, '-m', 'tempo_detection_claim_audit', str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_entry_point_configuration(self):
        config = (Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text()
        self.assertIn('tempo-detection-claim-audit = "tempo_detection_claim_audit.audit:main"', config)

    def test_built_distributions_and_entry_points(self):
        project = Path(__file__).resolve().parents[1]
        artifacts = self.root / 'artifacts'

        def execute(args, expected=0):
            result = subprocess.run([str(arg) for arg in args], cwd=str(self.root),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            return result.stdout

        execute([sys.executable, '-m', 'build', '--outdir', artifacts, project])
        distributions = sorted(artifacts.iterdir())
        self.assertEqual(len(distributions), 2)
        for index, distribution in enumerate(distributions):
            with self.subTest(distribution=distribution.name):
                env = self.root / ('env%d' % index)
                execute([sys.executable, '-m', 'venv', env])
                interpreter = env / 'bin' / 'python3'
                execute([interpreter, '-m', 'pip', 'install', '--no-cache-dir', distribution])
                if distribution.suffix == '.whl':
                    with zipfile.ZipFile(distribution) as archive:
                        name = next(n for n in archive.namelist() if n.endswith('/METADATA'))
                        readme = archive.read(name).decode('utf-8').split('\n\n', 1)[1]
                else:
                    with tarfile.open(distribution) as archive:
                        name = next(n for n in archive.getnames() if n.endswith('/README.md'))
                        readme = archive.extractfile(name).read().decode('utf-8')
                self.assertEqual(readme.rstrip(), (project / 'README.md').read_text().rstrip())
                self.assertEqual(claims(readme), (False, False))
                subject = self.root / ('subject%d' % index)
                subject.mkdir()
                (subject / 'README.md').write_text(readme)
                entries = ([interpreter, '-m', 'tempo_detection_claim_audit'],
                           [env / 'bin' / 'tempo-detection-claim-audit'])
                for entry in entries:
                    self.assertEqual(execute(entry + ['--version']).strip(), '0.1.0')
                    self.assertEqual(json.loads(execute(entry + [subject, '--json']))['verdict'], 'PASS')
                    (subject / 'README.md').write_text('Tempo detection')
                    finding = json.loads(execute(entry + [subject, '--json'], 2))['findings'][0]
                    self.assertEqual(finding['id'], 'documented-tempo-detection-claim-false')
                    self.assertEqual(finding['severity'], 'HIGH')
                    (subject / 'README.md').write_text(readme)

    def test_readme_self_audit(self):
        readme = Path(__file__).resolve().parents[1] / 'README.md'
        self.assertEqual(readme.read_text().splitlines()[0], '# Documentation claim auditor')
        self.assertEqual(claims(readme.read_text()), (False, False))
        self.write('README.md', readme.read_text())
        self.assertEqual(audit(self.root)['verdict'], 'PASS')


def _evidence_test(source, expected):
    def check(self):
        self.assertIs(evidence(source), expected)
        self.write('README.md', 'Tempo detection')
        self.write('app.py', source)
        self.assertEqual(self.run_cli()[0], 0 if expected else 2)
    return check


for _name, _source in GOOD.items():
    setattr(AuditTests, 'test_route_' + _name, _evidence_test(_source, True))
for _name, _source in BAD.items():
    setattr(AuditTests, 'test_not_evidence_' + _name, _evidence_test(_source, False))
for _name in GENERIC_NAMES:
    setattr(AuditTests, 'test_generic_attribute_' + _name,
            _evidence_test('engine.' + _name + '(audio)', True))
    setattr(AuditTests, 'test_generic_import_' + _name,
            _evidence_test('from engine import ' + _name + '\n' + _name + '(audio)', True))
    setattr(AuditTests, 'test_generic_alias_' + _name,
            _evidence_test('from engine import ' + _name + ' as run\nrun(audio)', True))
    if _name != 'beat_track':
        setattr(AuditTests, 'test_generic_shadow_' + _name,
                _evidence_test('def ' + _name + '(y): pass\nengine.' + _name + '(audio)', False))
# beat_track remains generic on an arbitrary base, but the library route has an explicit exception.
setattr(AuditTests, 'test_generic_shadow_beat_track',
        _evidence_test('def beat_track(y): pass\nengine.beat_track(audio)', False))

for _index, _phrase in enumerate(EXPECTED_TEMPO):
    def _positive(self, phrase=_phrase):
        self.assertEqual(claims(phrase), (True, False))
    setattr(AuditTests, 'test_positive_phrase_%02d' % _index, _positive)
for _index, _phrase in enumerate(EXPECTED_NONTEMPO):
    def _negative(self, phrase=_phrase):
        self.assertEqual(claims(phrase), (False, True))
    setattr(AuditTests, 'test_negative_phrase_%02d' % _index, _negative)


if __name__ == '__main__':
    unittest.main()
