import asyncio

from connector.client import _project_inventory_signature, _watch_project_inventory
from connector.local_store import LocalProjectStore
from connector.skills import SkillBinding, SkillManager


def test_project_signature_excludes_local_paths(tmp_path):
    project_dir = tmp_path / "private-repository"
    project_dir.mkdir()
    store = LocalProjectStore(str(tmp_path / "state.sqlite3"))
    try:
        project = store.add(str(project_dir), "Private")
        signature = _project_inventory_signature(store)
        assert signature == ((project.id, "Private", project.updated_at),)
        assert str(project_dir) not in repr(signature)
    finally:
        store.close()


def test_project_watcher_reports_changes_from_another_cli_process(tmp_path):
    async def scenario():
        state_path = str(tmp_path / "state.sqlite3")
        watched_store = LocalProjectStore(state_path)
        writer_store = LocalProjectStore(state_path)
        project_dir = tmp_path / "repo"
        project_dir.mkdir()

        class FakeConnector:
            def __init__(self):
                self.local_store = watched_store
                self.reports = []
                self.skill_reports = []

            async def report_projects(self, devbox_id):
                self.reports.append((
                    devbox_id,
                    [item.public_json() for item in self.local_store.list_projects()],
                ))

            async def report_skills(self, devbox_id):
                self.skill_reports.append((
                    devbox_id, SkillManager(self.local_store).inventory()))

        connector = FakeConnector()
        task = asyncio.create_task(
            _watch_project_inventory(connector, "devbox-1", interval=0.01))
        try:
            await asyncio.sleep(0.02)
            project = writer_store.add(str(project_dir), "Repo")
            for _ in range(50):
                if connector.reports:
                    break
                await asyncio.sleep(0.01)
            assert connector.reports == [(
                "devbox-1", [{"id": project.id, "name": "Repo"}],
            )]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            writer_store.close()
            watched_store.close()

    asyncio.run(scenario())



def test_project_watcher_reports_skill_changes_from_another_cli_process(tmp_path):
    async def scenario():
        state_path = str(tmp_path / "state.sqlite3")
        watched_store = LocalProjectStore(state_path)
        writer_store = LocalProjectStore(state_path)
        source = tmp_path / "alpha"
        source.mkdir()
        (source / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Alpha skill.\n---\n# Alpha\n",
            encoding="utf-8")

        class FakeConnector:
            def __init__(self):
                self.local_store = watched_store
                self.skill_reports = []

            async def report_projects(self, devbox_id):
                del devbox_id

            async def report_skills(self, devbox_id):
                self.skill_reports.append((
                    devbox_id, SkillManager(self.local_store).inventory()))

        connector = FakeConnector()
        task = asyncio.create_task(
            _watch_project_inventory(connector, "devbox-1", interval=0.01))
        try:
            await asyncio.sleep(0.02)
            SkillManager(writer_store).install(
                source,
                bindings=[SkillBinding("test-runtime", str(tmp_path / "target"))])
            for _ in range(50):
                if connector.skill_reports:
                    break
                await asyncio.sleep(0.01)
            assert len(connector.skill_reports) == 1
            devbox_id, skills = connector.skill_reports[0]
            assert devbox_id == "devbox-1"
            assert [skill["name"] for skill in skills] == ["alpha"]
            assert str(tmp_path) not in repr(skills)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            writer_store.close()
            watched_store.close()

    asyncio.run(scenario())
