import sys
import os
import unittest
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.library import LibraryHandlers
import webapp_state
import openbox

class DummyHandler(LibraryHandlers):
    def __init__(self):
        self.response_code = None
        self.response_json = None

    def send_json(self, code, payload):
        self.response_code = code
        self.response_json = payload

class TestSharedMedia(unittest.TestCase):
    def setUp(self):
        # We need a temporary openbox directory for DATA
        self.test_dir = tempfile.mkdtemp()
        self.media_root = Path(self.test_dir) / "media"
        self.media_root.mkdir(parents=True)
        
        # Override DATA to use our test directory
        self.old_data = openbox.DATA
        openbox.DATA = Path(self.test_dir) / "openbox" / "openbox.json"
        
        self.old_media_roots_env = os.environ.get(webapp_state.MEDIA_ROOTS_ENV)
        os.environ[webapp_state.MEDIA_ROOTS_ENV] = str(self.media_root)
        
        # Initialize an empty state
        webapp_state.STATE_STORE.save({"games": [], "settings": {}})
        
        # We also need a mocked handler
        self.handler = DummyHandler()

    def tearDown(self):
        shutil.rmtree(self.test_dir)
        openbox.DATA = self.old_data
        
        if self.old_media_roots_env is None:
            os.environ.pop(webapp_state.MEDIA_ROOTS_ENV, None)
        else:
            os.environ[webapp_state.MEDIA_ROOTS_ENV] = self.old_media_roots_env
            
        webapp_state.STATE_STORE.save({"games": [], "settings": {}})

    def _create_media_file(self, name):
        path = self.media_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dummy")
        return str(path)

    def test_shared_cover_retained(self):
        # Two games share a cover
        cover_path = self._create_media_file("shared_cover.jpg")
        
        def mutate(state):
            state["games"] = [
                {"game_id": "game1", "name": "Game 1", "cover": cover_path},
                {"game_id": "game2", "name": "Game 2", "cover": cover_path}
            ]
            return None
        webapp_state.update_state_with_result(mutate)
        
        self.handler.delete_game({"game_id": "game1", "delete_media": True})
        
        self.assertEqual(self.handler.response_code, 200)
        self.assertEqual(self.handler.response_json["removed"], "Game 1")
        self.assertEqual(self.handler.response_json["deleted_media"], [])
        self.assertEqual(self.handler.response_json["shared_media"], [cover_path])
        self.assertTrue(os.path.exists(cover_path))

    def test_screenshots_shared_across_games(self):
        shared_shot = self._create_media_file("shared.png")
        unique_shot = self._create_media_file("unique.png")
        
        def mutate(state):
            state["games"] = [
                {"game_id": "game1", "name": "Game 1", "screenshots": [shared_shot, unique_shot]},
                {"game_id": "game2", "name": "Game 2", "screenshots": [shared_shot]}
            ]
            return None
        webapp_state.update_state_with_result(mutate)
        
        self.handler.delete_game({"game_id": "game1", "delete_media": True})
        
        self.assertEqual(self.handler.response_json["deleted_media"], [unique_shot])
        self.assertEqual(self.handler.response_json["shared_media"], [shared_shot])
        self.assertTrue(os.path.exists(shared_shot))
        self.assertFalse(os.path.exists(unique_shot))

    def test_duplicate_path_spellings(self):
        cover_path = self._create_media_file("dup_cover.jpg")
        
        def mutate(state):
            # simulate different path spellings pointing to the same file
            # os.path.realpath will resolve these
            diff_spelling = os.path.join(self.media_root, ".", "dup_cover.jpg")
            state["games"] = [
                {"game_id": "game1", "name": "Game 1", "cover": cover_path},
                {"game_id": "game2", "name": "Game 2", "cover": diff_spelling}
            ]
            return None
        webapp_state.update_state_with_result(mutate)
        
        self.handler.delete_game({"game_id": "game1", "delete_media": True})
        
        self.assertTrue(os.path.exists(cover_path))
        self.assertEqual(self.handler.response_json["deleted_media"], [])
        self.assertIn(cover_path, self.handler.response_json["shared_media"])

    def test_media_outside_approved_roots(self):
        # create a file outside media root
        outside_dir = Path(self.test_dir) / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "outside.jpg"
        outside_file.write_text("dummy")
        
        def mutate(state):
            state["games"] = [
                {"game_id": "game1", "name": "Game 1", "cover": str(outside_file)},
            ]
            return None
        webapp_state.update_state_with_result(mutate)
        
        self.handler.delete_game({"game_id": "game1", "delete_media": True})
        
        # should skip deleting since it's unapproved
        self.assertEqual(self.handler.response_json["deleted_media"], [])
        self.assertEqual(self.handler.response_json["shared_media"], [])
        self.assertTrue(os.path.exists(outside_file))

    def test_no_shared_media(self):
        unique_cover = self._create_media_file("unique_cover.jpg")
        def mutate(state):
            state["games"] = [
                {"game_id": "game1", "name": "Game 1", "cover": unique_cover},
                {"game_id": "game2", "name": "Game 2", "cover": self._create_media_file("other.jpg")}
            ]
            return None
        webapp_state.update_state_with_result(mutate)
        
        self.handler.delete_game({"game_id": "game1", "delete_media": True})
        
        self.assertEqual(self.handler.response_json["deleted_media"], [unique_cover])
        self.assertEqual(self.handler.response_json["shared_media"], [])
        self.assertFalse(os.path.exists(unique_cover))

    def test_delete_without_delete_media_flag(self):
        cover = self._create_media_file("cover.jpg")
        def mutate(state):
            state["games"] = [
                {"game_id": "game1", "name": "Game 1", "cover": cover},
            ]
            return None
        webapp_state.update_state_with_result(mutate)
        
        self.handler.delete_game({"game_id": "game1"})
        
        self.assertEqual(self.handler.response_json["deleted_media"], [])
        self.assertEqual(self.handler.response_json["shared_media"], [])
        self.assertTrue(os.path.exists(cover))

if __name__ == "__main__":
    unittest.main()
