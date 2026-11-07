"""Работа с приватным репозиторием ссылок через GitHub API.
Все изменения записываются одним коммитом — роутеры увидят один новый коммит."""
import aiohttp

from config import GH_REPO, GH_TOKEN
from links import DIR

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token: str = GH_TOKEN, repo: str = GH_REPO, api: str = API):
        self.repo, self.api = repo, api
        self.headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28",
                        "Accept": "application/vnd.github+json"}

    async def _req(self, method: str, path: str, raw: bool = False, ok404: bool = False, **kw):
        url = f"{self.api}/repos/{self.repo}{path}"
        headers = dict(self.headers)
        if raw:
            headers["Accept"] = "application/vnd.github.raw"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.request(method, url, headers=headers, **kw) as r:
                if ok404 and r.status in (404, 409):  # 409 — репозиторий пустой
                    return None
                if r.status >= 400:
                    text = await r.text()
                    raise GitHubError(f"GitHub {r.status} на {method} {path}: {text[:200]}")
                return await r.text() if raw else await r.json()

    async def _head(self) -> tuple[str, str, str] | None:
        """(ветка, sha коммита, sha дерева) или None для пустого репозитория."""
        repo = await self._req("GET", "")
        branch = repo.get("default_branch") or "main"
        ref = await self._req("GET", f"/git/ref/heads/{branch}", ok404=True)
        if not ref:
            return None
        commit = await self._req("GET", f"/git/commits/{ref['object']['sha']}")
        return branch, ref["object"]["sha"], commit["tree"]["sha"]

    async def read_state(self) -> tuple[dict[str, list[str]], list[str]]:
        """({имя роутера: [ссылки]}, [старые файлы slotN.txt в корне])."""
        head = await self._head()
        if not head:
            return {}, []
        tree = await self._req("GET", f"/git/trees/{head[2]}?recursive=1")
        routers, legacy = {}, []
        for item in tree.get("tree", []):
            p = item["path"]
            if item["type"] != "blob":
                continue
            if p.startswith(DIR + "/") and p.endswith(".txt") and p.count("/") == 1:
                text = await self._req("GET", f"/git/blobs/{item['sha']}", raw=True)
                routers[p[len(DIR) + 1:-4]] = [ln.strip() for ln in text.splitlines() if ln.strip()]
            elif "/" not in p and p.startswith("slot") and p.endswith(".txt"):
                legacy.append(p)
        return routers, legacy

    async def commit(self, files: dict[str, str | None], message: str) -> str:
        """Записать изменения одним коммитом. Возвращает sha коммита."""
        head = await self._head()
        if not head:  # пустой репозиторий: первый коммит через contents API
            import base64
            await self._req("PUT", "/contents/README.md", json={
                "message": "init", "content": base64.b64encode(b"Links for routers (podkop-bot)\n").decode()})
            head = await self._head()
        branch, parent, base_tree = head
        tree = [{"path": p, "mode": "100644", "type": "blob", **({"content": c} if c is not None else {"sha": None})}
                for p, c in files.items()]
        new_tree = await self._req("POST", "/git/trees", json={"base_tree": base_tree, "tree": tree})
        commit = await self._req("POST", "/git/commits",
                                 json={"message": message, "tree": new_tree["sha"], "parents": [parent]})
        await self._req("PATCH", f"/git/refs/heads/{branch}", json={"sha": commit["sha"]})
        return commit["sha"]

    async def last_commit(self) -> tuple[str, str] | None:
        """(дата ISO, сообщение) последнего коммита."""
        r = await self._req("GET", "/commits?per_page=1", ok404=True)
        if not r:
            return None
        c = r[0]["commit"]
        return c["committer"]["date"], c["message"]

    async def check(self) -> bool:
        """True, если репозиторий приватный (бросает GitHubError при отсутствии доступа)."""
        repo = await self._req("GET", "")
        return bool(repo.get("private"))
