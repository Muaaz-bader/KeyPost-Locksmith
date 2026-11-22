import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask
from PIL import Image

from automation import ad_research, listing_store, manual_backups


def listing(listing_id, age_group='new', age_days=0, **extra):
    item = {'id': listing_id, 'url': f'https://www.facebook.com/marketplace/item/{listing_id}/',
            'title': f'Locksmith {listing_id}', 'price': '1', 'price_text': '$1', 'description': 'Call us',
            'location': 'Dallas, TX', 'listed_info': '', 'age_group': age_group, 'age_days': age_days,
            'photos': ['photo_01.jpg'], 'phone_in_text': False, 'phone_in_photo': False}
    item.update(extra)
    return item


class AgeTests(unittest.TestCase):
    def test_listing_page_dates(self):
        cases = {
            'Listed 56 minutes ago in Dallas, TX': ('new', 0),
            'Listed 4 hours ago': ('new', 0),
            'Listed 2 days ago in Dallas, TX': ('new', 2),
            'Listed 6 days ago in Dallas, TX': ('new', 6),
            'Listed a week ago': ('1_week', 7),
            'Listed 1 week ago': ('1_week', 7),
            'Listed 2 weeks ago in Dallas, TX': ('2_weeks', 14),
            'Listed 3 weeks ago': ('3_weeks', 21),
            'Listed yesterday': ('new', 1),
        }
        for text, expected in cases.items():
            self.assertEqual(ad_research.parse_age(text), expected, text)

    def test_no_listed_line_is_shop_item_never_old(self):
        self.assertEqual(ad_research.parse_age('', title_read=True, price_read=True), ('shop', -1))
        self.assertFalse(ad_research.is_best(5, 'shop'))
        self.assertEqual(ad_research.parse_age('', title_read=True, price_read=False), ('unread', -1))
        self.assertEqual(ad_research.parse_age('', title_read=False, price_read=True), ('unread', -1))

    def test_city_without_time_is_oldest(self):
        # Seen on live Facebook: older listings show the city but no time.
        self.assertEqual(ad_research.parse_age('Listed in Forney, TX'), ('oldest', 28))
        self.assertEqual(ad_research.parse_age('Listed in Dallas, TX', price_read=False), ('unread', -1))
        self.assertEqual(ad_research.parse_age('Listed 4 weeks ago in Richardson, TX'), ('oldest', 28))
        self.assertEqual(ad_research.parse_age('Listed about an hour ago in Mesquite, TX'), ('new', 0))

    def test_unrecognised_date_is_not_counted_as_old(self):
        self.assertEqual(ad_research.parse_age('Listed recently-ish'), ('unread', -1))

    def test_accessibility_tree_joins_split_text(self):
        names = ['Locksmith', '$1', 'Listed', '6 days ago', 'in', 'Dallas, TX', 'Details']
        self.assertEqual(ad_research.listed_from_ax_names(names), 'Listed 6 days ago in Dallas, TX')
        self.assertEqual(ad_research.listed_from_ax_names(['Listed 2 weeks ago']), 'Listed 2 weeks ago')
        self.assertEqual(ad_research.listed_from_ax_names(['Seller details', 'Message']), '')


class ScoringTests(unittest.TestCase):
    def test_stars_and_best(self):
        self.assertEqual([ad_research.stars_for(n) for n in (1, 2, 5, 7)], [1, 2, 5, 5])
        self.assertTrue(ad_research.is_best(3, 'oldest'))
        self.assertFalse(ad_research.is_best(2, 'oldest'))
        self.assertFalse(ad_research.is_best(6, '3_weeks'))

    def test_merge_rank_and_summary(self):
        keywords = ['car lockout', 'key programming', 'house rekey', 'safe']
        hits = {'car lockout': ['1', '2', '3'], 'key programming': ['1', '2'],
                'house rekey': ['1', '4'], 'safe': []}
        items = [listing('1', 'oldest', 28, phone_in_text=True), listing('2', '3_weeks', 21, price='150'),
                 listing('3', 'new', 2), listing('4', 'oldest', 28)]
        result = ad_research.build_results(keywords, hits, items, ocr_checked=False)
        ranked = result['listings']
        self.assertEqual([item['id'] for item in ranked], ['1', '2', '4', '3'])
        self.assertEqual(ranked[0]['keywords'], ['car lockout', 'key programming', 'house rekey'])
        self.assertEqual((ranked[0]['stars'], ranked[0]['best']), (3, True))
        self.assertFalse(ranked[1]['best'])
        summary = result['summary']
        self.assertEqual((summary['total_results'], summary['unique'], summary['best'], summary['multi_keyword']),
                         (7, 4, 1, 2))
        self.assertEqual(summary['basis'], 'best')
        self.assertEqual(summary['basis_traits']['phone_in_text_pct'], 100)
        self.assertEqual(summary['basis_traits']['placeholder_price_pct'], 100)
        stats = {row['keyword']: row for row in result['keyword_stats']}
        self.assertEqual(stats['car lockout'], {'keyword': 'car lockout', 'results': 3, 'overlap': 2})
        self.assertEqual(stats['safe']['results'], 0)

    def test_summary_falls_back_when_no_best(self):
        result = ad_research.build_results(['a', 'b'], {'a': ['1', '2'], 'b': ['1']},
                                           [listing('1'), listing('2')], ocr_checked=True)
        self.assertEqual(result['summary']['basis'], 'multi')
        self.assertEqual(result['summary']['basis_traits']['count'], 1)

    def test_photos_found_when_viewer_opens_on_video(self):
        clicked = []

        def execute(script, *args):
            if script == ad_research.extractor._THUMBNAILS_JS:
                return ['video-thumb', 'photo-thumb']
            if script.startswith('arguments[0].click'):
                clicked.append(args[0])
                return None
            if script == ad_research.extractor._LARGE_PHOTO_JS:
                return 'https://scontent.fbcdn.net/photo.jpg' if 'photo-thumb' in clicked else ''
            return None

        driver = MagicMock()
        driver.execute_script.side_effect = execute
        with patch.object(ad_research.extractor, '_photo_urls', return_value=['p1', 'p2']), \
             patch.object(ad_research, '_sleep'):
            self.assertEqual(ad_research._photos_past_video(driver), ['p1', 'p2'])
        self.assertEqual(clicked, ['video-thumb', 'photo-thumb'])

    def test_phone_detection(self):
        self.assertTrue(ad_research.has_phone('Call (214) 555-0199 now'))
        self.assertTrue(ad_research.has_phone('214.555.0199'))
        self.assertFalse(ad_research.has_phone('Open 24/7, 30 min arrival'))

    def test_input_validation(self):
        keywords, location, count = ad_research._clean_inputs([' car  lockout ', '', 'Car Lockout', 'rekey'], ' Dallas ', '5')
        self.assertEqual((keywords, location, count), (['car lockout', 'rekey'], 'Dallas', 5))
        with self.assertRaises(ValueError):
            ad_research._clean_inputs(['', ' '], '', 5)
        with self.assertRaises(ValueError):
            ad_research._clean_inputs(['a'], '', 41)
        with self.assertRaises(ValueError):
            ad_research._clean_inputs([str(i) for i in range(11)], '', 5)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        patcher = patch.object(ad_research, 'data_dir', side_effect=self._dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.folder.cleanup)
        ad_research._stop.clear()

    def _dir(self, *parts):
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run_job(self, location='Austin, Texas', collect=None):
        driver = MagicMock()
        lock = MagicMock()
        calls = []
        with patch.object(ad_research, 'open_posting_tab', return_value=(driver, 'tab', set(), lock)), \
             patch.object(ad_research, 'close_posting_tab') as close, \
             patch.object(ad_research, '_open_search'), \
             patch.object(ad_research, '_read_location', return_value='Dallas, Texas'), \
             patch.object(ad_research, '_set_location', side_effect=lambda d, value: calls.append(value)), \
             patch.object(ad_research, '_wait_quietly'), \
             patch.object(ad_research, '_collect_ids', side_effect=collect or (lambda d, n: ['1', '2'])), \
             patch.object(ad_research, '_read_one', side_effect=lambda d, i, s: listing(i, 'oldest', 28)):
            ad_research._run('acct', ['car lockout', 'rekey'], location, 5)
        return calls, close

    def test_location_is_noted_set_and_restored(self):
        calls, close = self.run_job()
        self.assertEqual(calls, ['Austin, Texas', 'Dallas, Texas'])
        close.assert_called_once()
        results = ad_research.load_results()
        self.assertEqual(len(results['listings']), 2)
        self.assertEqual(results['listings'][0]['appearances'], 2)
        self.assertEqual(ad_research.status()['stage'], 'Finished')

    def test_location_restored_after_stop(self):
        def collect(driver, limit):
            ad_research._stop.set()
            raise InterruptedError('Stopped by user.')
        calls, close = self.run_job(collect=collect)
        self.assertEqual(calls, ['Austin, Texas', 'Dallas, Texas'])
        close.assert_called_once()
        self.assertEqual(ad_research.status()['stage'], 'Stopped')

    def test_location_untouched_when_empty(self):
        calls, _ = self.run_job(location='')
        self.assertEqual(calls, [])


class BackupAndRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch('automation.maintenance.start_scheduler_without_maintenance'):
            cls.routes = importlib.import_module('web.routes')
        root = Path(__file__).resolve().parents[1]
        cls.app = Flask(__name__, template_folder=str(root / 'web/templates'), static_folder=str(root / 'web/static'))
        cls.app.secret_key = 'test'
        cls.app.register_blueprint(cls.routes.web_bp)
        cls.app.testing = True

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        for target in (patch.object(ad_research, 'data_dir', side_effect=lambda *parts: self._dir(*parts)),
                       patch.object(listing_store, 'data_dir', return_value=self.root),
                       patch.object(manual_backups, 'data_dir', side_effect=lambda *parts: self._dir(*parts)),
                       patch.object(self.routes, 'get_active_account', return_value='research-account'),
                       patch.object(self.routes, 'get_all_accounts', return_value=['research-account']),
                       patch.object(self.routes, '_portal_auth_is_configured', return_value=True)):
            target.start()
            self.addCleanup(target.stop)
        self.addCleanup(self.folder.cleanup)
        photos = self.root / 'ad_research' / 'photos' / '111'
        photos.mkdir(parents=True)
        Image.new('RGB', (40, 30), '#d9a441').save(photos / 'photo_01.jpg')
        result = ad_research.build_results(['a', 'b', 'c'], {'a': ['111'], 'b': ['111'], 'c': ['111']},
                                           [listing('111', 'oldest', 28)], ocr_checked=False)
        result.update(account='research-account', keywords=['a', 'b', 'c'], location='', per_keyword=5,
                      finished_at='2026-11-22 10:00', stopped=False, errors=[], saved={})
        ad_research._save_results(result)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['portal_logged_in'] = True

    def _dir(self, *parts):
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_add_to_backup_keeps_photo_and_mark(self):
        response = self.client.post('/ad_research/backup/111')
        self.assertEqual(response.status_code, 200, response.json)
        saved = next(item for item in listing_store.load('research-account')['listings']
                     if item['id'] == response.json['record_id'])
        self.assertTrue(saved['extracted'])
        self.assertEqual(saved['research']['stars'], 3)
        self.assertTrue(saved['research']['best'])
        self.assertTrue(Path(saved['downloaded_images'][0]['path']).is_file())
        # Clearing research results must not remove the saved backup's photo.
        ad_research.clear()
        self.assertTrue(Path(saved['downloaded_images'][0]['path']).is_file())
        self.assertIsNone(ad_research.load_results())

    def test_add_twice_is_refused(self):
        self.assertEqual(self.client.post('/ad_research/backup/111').status_code, 200)
        self.assertEqual(self.client.post('/ad_research/backup/111').status_code, 400)

    def test_page_status_and_photo_routes(self):
        page = self.client.get('/ad_research')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Do not use your main posting account for research', html)
        self.assertIn('href="/ad_research">Ad Research</a>', html)
        self.assertEqual(html.count('aria-label="Keyword '), 10)
        status = self.client.get('/ad_research/status').json
        self.assertEqual(status['results']['summary']['best'], 1)
        photo = self.client.get('/ad_research/photo/111/photo_01.jpg')
        self.assertEqual(photo.status_code, 200)
        photo.close()
        self.assertEqual(self.client.get('/ad_research/photo/111/..%2Fresults.json').status_code, 404)
        self.assertEqual(self.client.get('/ad_research/photo/abc/photo_01.jpg').status_code, 404)

    def test_backup_posts_shows_research_mark(self):
        self.client.post('/ad_research/backup/111')
        html = self.client.get('/backup_listings').get_data(as_text=True)
        self.assertIn('backup-research-mark', html)
        self.assertIn('BEST', html)


if __name__ == '__main__':
    unittest.main()
