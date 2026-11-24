import importlib.util
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'BrightT_custom_export_SP.py'
spec = importlib.util.spec_from_file_location('plugin', SOURCE)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

NORMAL = {'fileName': '$textureSet_Normal(.$udim)', 'channels': [
    {'destChannel': c, 'srcChannel': c, 'srcMapType': 'virtualMap', 'srcMapName': 'Normal_OpenGL'}
    for c in 'RGB']}
ORM = {'fileName': '$textureSet_ORM', 'channels': [
    {'destChannel': 'R', 'srcChannel': 'L', 'srcMapType': 'virtualMap', 'srcMapName': 'AO_Mixed', 'srcPath': ''},
    {'destChannel': 'G', 'srcChannel': 'L', 'srcMapType': 'documentMap', 'srcMapName': 'roughness'},
    {'destChannel': 'B', 'srcChannel': 'L', 'srcMapType': 'documentMap', 'srcMapName': 'metallic'},
    {'destChannel': 'A', 'srcChannel': 'L', 'srcMapType': 'documentMap', 'srcMapName': 'opacity'}],
    'parameters': {'fileFormat': 'tif', 'bitDepth': '16', 'dithering': True, 'paddingAlgorithm': 'diffusion', 'dilationDistance': 12},
    'customMetadata': {'keep': True}}


class ExportTests(unittest.TestCase):
    def test_restore_order_by_identity(self):
        a = dict(self.row(tile=[0, 0]), material='sm_AB_ABC')
        b = dict(self.row(tile=[1, 0]), material='sm_AB_ABC')
        c = dict(self.row(tile=[2, 0]), material='sm_AB_ABC')
        restored = p.restore_rows([a, b, c], [dict(b, base='first', enabled=False), dict(a, base='second')])
        self.assertEqual([r['tile'] for r in restored], [[1, 0], [0, 0], [2, 0]])
        self.assertEqual(restored[0]['base'], 'first')
        self.assertFalse(restored[0]['enabled'])

    def test_profile_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'settings/profiles.json'
            self.assertEqual(p.read_profiles(path)['profiles'], {})
            data = {'version': 1, 'default': '默认', 'profiles': {'默认': {'overwrite': True, 'format': 'png'}}}
            p.write_json_atomic(path, data)
            self.assertEqual(p.read_profiles(path), data)

    def test_invalid_profile_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'profiles.json'
            path.write_text('{"version":99,"profiles":{}}')
            with self.assertRaises(ValueError):
                p.read_profiles(path)

    def test_overwrite_success_and_unrelated_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            stage, dest = Path(root) / 'stage', Path(root) / 'dest'
            stage.mkdir(); dest.mkdir()
            (stage / 'a.png').write_bytes(b'new')
            (dest / 'a.png').write_bytes(b'old')
            (dest / 'unrelated.png').write_bytes(b'keep')
            p.publish_files(stage, dest, ['a.png'], overwrite=True)
            self.assertEqual((dest / 'a.png').read_bytes(), b'new')
            self.assertEqual((dest / 'unrelated.png').read_bytes(), b'keep')

    def test_overwrite_rollback_on_publish_error(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            stage, dest = Path(root) / 'stage', Path(root) / 'dest'
            stage.mkdir(); dest.mkdir()
            for name in ['a.png', 'b.png']:
                (stage / name).write_bytes(b'new')
                (dest / name).write_bytes(b'old-' + name.encode())
            real_replace = p.os.replace
            def fail_second(source, target):
                if Path(source) == stage / 'b.png':
                    raise OSError('simulated disk error')
                return real_replace(source, target)
            with patch.object(p.os, 'replace', side_effect=fail_second):
                with self.assertRaises(OSError):
                    p.publish_files(stage, dest, ['a.png', 'b.png'], overwrite=True)
            for name in ['a.png', 'b.png']:
                self.assertEqual((dest / name).read_bytes(), b'old-' + name.encode())

    def test_overwrite_missing_output_does_not_touch_originals(self):
        with tempfile.TemporaryDirectory() as root:
            stage, dest = Path(root) / 'stage', Path(root) / 'dest'
            stage.mkdir(); dest.mkdir()
            (stage / 'a.png').write_bytes(b'new')
            (dest / 'a.png').write_bytes(b'old')
            with self.assertRaises(ValueError):
                p.publish_files(stage, dest, ['a.png', 'missing.png'], overwrite=True)
            self.assertEqual((dest / 'a.png').read_bytes(), b'old')

    def test_overwrite_rejects_directory(self):
        with tempfile.TemporaryDirectory() as root:
            stage, dest = Path(root) / 'stage', Path(root) / 'dest'
            stage.mkdir(); dest.mkdir()
            (stage / 'a.png').write_bytes(b'new')
            (dest / 'a.png').mkdir()
            with self.assertRaises(ValueError):
                p.publish_files(stage, dest, ['a.png'], overwrite=True)

    def row(self, base='sm_AB01_ABC', tile=None, root='sm_AB_ABC'):
        return {'base': base, 'tile': tile, 'root': root}

    def test_single_material(self):
        base = p.numbered_name('sm_AB_ABC', 1)
        self.assertEqual(p.expected_names([self.row(base)], [(NORMAL, 'N')], 'png'), ['sm_AB01_ABC_N.png'])

    def test_two_materials(self):
        rows = [self.row(p.numbered_name(name, i + 1), root=name)
                for i, name in enumerate(['sm_AB_ABC', 'sm_AB_ABD'])]
        self.assertEqual(p.expected_names(rows, [(NORMAL, 'N')], 'png'),
                         ['sm_AB01_ABC_N.png', 'sm_AB02_ABD_N.png'])

    def test_two_udims(self):
        rows = [self.row(p.numbered_name('sm_AB_ABC', i + 1), [i, 0]) for i in range(2)]
        configs = [p.make_config(r, [(NORMAL, 'N')], 'C:/export') for r in rows]
        self.assertEqual([c['exportList'][0]['filter']['uvTiles'] for c in configs], [[[0, 0]], [[1, 0]]])
        self.assertEqual([c['exportPresets'][0]['maps'][0]['fileName'] for c in configs],
                         ['sm_AB01_ABC_N', 'sm_AB02_ABC_N'])

    def test_non_udim_no_filter(self):
        c = p.make_config(self.row(), [(NORMAL, 'N')], 'C:/export')
        self.assertNotIn('filter', c['exportList'][0])
        self.assertEqual(c['exportList'][0]['rootPath'], 'sm_AB_ABC')

    def test_udim_later_row_and_stack(self):
        c = p.make_config(self.row(tile=[0, 1], root='body/mask'), [(NORMAL, 'N')], 'C:/export')
        self.assertEqual(c['exportList'], [{'rootPath': 'body/mask', 'filter': {'uvTiles': [[0, 1]]}}])

    def test_custom_names_and_suffixes(self):
        self.assertEqual(p.expected_names([self.row('任意名字')], [(NORMAL, 'Normal'), (ORM, 'Color')], 'tif'),
                         ['任意名字_Normal.tif', '任意名字_Color.tif'])

    def test_collision_case_insensitive(self):
        with self.assertRaises(ValueError):
            p.expected_names([self.row('TEST'), self.row('test')], [(NORMAL, 'N')], 'png')
        with self.assertRaises(ValueError):
            p.expected_names([self.row()], [(NORMAL, 'N'), (ORM, 'n')], 'png')

    def test_illegal_names(self):
        for name in ['', '../escape', 'a\\b', 'a:b', '$udim', 'a*', 'a?', ' a', 'a.', 'CON', 'NUL.txt']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                p.validate_name(name)

    def test_export_parameters_and_normal(self):
        c = p.make_config(self.row(), [(NORMAL, 'N'), (ORM, 'ORM')], 'C:/export', 'png', '8', 12)
        maps = c['exportPresets'][0]['maps']
        self.assertEqual(maps[0]['channels'], NORMAL['channels'])
        self.assertEqual(maps[1]['channels'], ORM['channels'])
        self.assertEqual(maps[1]['parameters']['sizeLog2'], 12)
        self.assertEqual(maps[1]['parameters']['bitDepth'], '8')
        self.assertFalse(c['exportShaderParams'])

    def test_native_rgba_preserved_without_mutation(self):
        import copy
        original = copy.deepcopy(ORM)
        maps = p.renamed_maps(self.row(), [(ORM, 'ORM')])
        self.assertEqual(maps[0]['channels'], original['channels'])
        self.assertEqual(maps[0]['parameters'], original['parameters'])
        self.assertEqual(maps[0]['customMetadata'], original['customMetadata'])
        self.assertEqual(maps[0]['fileName'], 'sm_AB01_ABC_ORM')
        maps[0]['channels'][0]['srcPath'] = 'changed'
        self.assertEqual(ORM, original)

    def test_mixed_format_preview(self):
        self.assertEqual(p.expected_names([self.row()], [(NORMAL, 'N'), (ORM, 'ORM')]),
                         ['sm_AB01_ABC_N.png', 'sm_AB01_ABC_ORM.tif'])

    def test_preview_equals_export_config(self):
        for tile in [None, [0, 0], [1, 0], [0, 1]]:
            row = self.row(tile=tile)
            maps = [(NORMAL, 'N'), (ORM, 'Packed')]
            config = p.make_config(row, maps, 'C:/export', 'png', '16')
            actual = [m['fileName'] + '.' + m['parameters']['fileFormat']
                      for m in config['exportPresets'][0]['maps']]
            self.assertEqual(p.expected_names([row], maps, 'png', '16'), actual)

    def test_suffix_inference(self):
        self.assertEqual(p.suggest_suffix('$textureSet_Normal(.$udim)', 0), 'Normal')
        self.assertEqual(p.suggest_suffix('$textureSet_ORM', 1), 'ORM')
        self.assertEqual(p.suggest_suffix('$textureSet', 2), 'Map03')

    def test_channel_readout(self):
        self.assertIn('AO_Mixed:L', p.channel_summary(ORM, 'R'))
        self.assertIn('roughness:L', p.channel_summary(ORM, 'G'))
        self.assertIn('metallic:L', p.channel_summary(ORM, 'B'))
        self.assertIn('opacity:L', p.channel_summary(ORM, 'A'))
        self.assertEqual(p.channel_summary(NORMAL, 'A'), '—')

    def test_tga_invalid_depth(self):
        with self.assertRaises(ValueError):
            p.make_config(self.row(), [(ORM, 'ORM')], 'C:/export', 'tga')
        self.assertEqual(p.expected_names([self.row()], [(ORM, 'ORM')], 'tga', '8'),
                         ['sm_AB01_ABC_ORM.tga'])

    def test_no_selection(self):
        with self.assertRaises(ValueError):
            p.expected_names([], [(NORMAL, 'N')], 'png')
        with self.assertRaises(ValueError):
            p.make_config(self.row(), [], 'C:/export')

    def test_numbering_controls(self):
        self.assertEqual(p.numbered_name('sm_AB_ABC', 7, 2, 3), 'sm_AB007_ABC')
        with self.assertRaises(ValueError):
            p.numbered_name('plain', 1)

    def test_publish_success(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            stage, dest = root / 'stage', root / 'dest'
            stage.mkdir(); dest.mkdir()
            (stage / 'a.png').write_bytes(b'texture')
            p.publish_files(stage, dest, ['a.png'])
            self.assertEqual((dest / 'a.png').read_bytes(), b'texture')

    def test_existing_file_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            stage, dest = root / 'stage', root / 'dest'
            stage.mkdir(); dest.mkdir()
            for name in ['a.png', 'b.png']:
                (stage / name).write_bytes(b'new')
            (dest / 'b.png').write_bytes(b'original')
            with self.assertRaises(FileExistsError):
                p.publish_files(stage, dest, ['a.png', 'b.png'])
            self.assertFalse((dest / 'a.png').exists())
            self.assertEqual((dest / 'b.png').read_bytes(), b'original')

    def test_missing_or_empty_output_rollback(self):
        for empty in [False, True]:
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                stage, dest = root / 'stage', root / 'dest'
                stage.mkdir(); dest.mkdir()
                (stage / 'a.png').write_bytes(b'new')
                if empty:
                    (stage / 'b.png').touch()
                with self.assertRaises(ValueError):
                    p.publish_files(stage, dest, ['a.png', 'b.png'])
                self.assertFalse(list(dest.iterdir()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
