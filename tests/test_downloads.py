from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from bp_work_server.api import create_app
from bp_work_server.store import WorkStore


def make_store(tmp_path) -> WorkStore:
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    return store


def make_zip(marker: bytes = b"exe") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Burnout5.exe", marker)
        zf.writestr("asset.dat", b"asset bytes")
    return buf.getvalue()


def client_with_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_DOWNLOADS_DIR", str(tmp_path / "downloads"))
    store = make_store(tmp_path)
    admin = store.create_worker("Adriwin", is_admin=True)
    user = store.create_worker("JeBobs")
    return TestClient(create_app(store)), admin, user


def upload(client, token, zip_bytes, *, commit="a" * 40, assets="deadbeef", built_at=None):
    data = {"commit_sha": commit, "commit_short": commit[:12], "branch": "dev",
            "asset_manifest_hash": assets}
    if built_at:
        data["built_at"] = built_at
    return client.post(
        "/admin/builds",
        headers={"X-Work-Token": token},
        files={"file": ("build.zip", zip_bytes, "application/zip")},
        data=data,
    )


def test_upload_requires_admin(tmp_path, monkeypatch):
    client, admin, user = client_with_downloads(tmp_path, monkeypatch)
    z = make_zip()

    # no token -> 401
    assert upload(client, "", z).status_code == 401
    # non-admin -> 403
    assert upload(client, user["token"], z).status_code == 403
    # admin -> 201
    assert upload(client, admin["token"], z).status_code == 201


def test_publish_then_download_roundtrip(tmp_path, monkeypatch):
    client, admin, _ = client_with_downloads(tmp_path, monkeypatch)
    z = make_zip(b"the-exe")

    created = upload(client, admin["token"], z)
    assert created.status_code == 201
    body = created.json()
    assert body["commit_short"] == "a" * 12
    assert body["size_bytes"] == len(z)
    assert body["download_url"] == f"/download/{body['id']}"

    # /api/builds surfaces it as the latest
    listing = client.get("/api/builds").json()
    assert listing["latest"]["id"] == body["id"]
    assert len(listing["builds"]) == 1

    # both download routes return the exact bytes
    latest = client.get("/download/latest")
    assert latest.status_code == 200
    assert latest.content == z
    assert "attachment" in latest.headers.get("content-disposition", "")

    specific = client.get(f"/download/{body['id']}")
    assert specific.status_code == 200
    assert specific.content == z


def test_empty_build_rejected(tmp_path, monkeypatch):
    client, admin, _ = client_with_downloads(tmp_path, monkeypatch)
    resp = upload(client, admin["token"], b"")
    assert resp.status_code == 400


def test_no_build_yet_is_404_not_500(tmp_path, monkeypatch):
    client, _, _ = client_with_downloads(tmp_path, monkeypatch)
    assert client.get("/download/latest").status_code == 404
    assert client.get("/download/999").status_code == 404
    empty = client.get("/api/builds").json()
    assert empty["latest"] is None
    assert empty["builds"] == []


def test_download_increments_counter(tmp_path, monkeypatch):
    client, admin, _ = client_with_downloads(tmp_path, monkeypatch)
    bid = upload(client, admin["token"], make_zip()).json()["id"]

    assert client.get("/api/builds").json()["latest"]["downloads"] == 0

    # a fresh GET (no Range) counts
    client.get("/download/latest")
    assert client.get("/api/builds").json()["latest"]["downloads"] == 1
    client.get(f"/download/{bid}")
    assert client.get("/api/builds").json()["latest"]["downloads"] == 2

    # a mid-file range request (resume/segment) does NOT double-count
    client.get(f"/download/{bid}", headers={"Range": "bytes=5-9"})
    assert client.get("/api/builds").json()["latest"]["downloads"] == 2

    # a range starting at 0 (how some browsers begin) counts as a fresh start
    client.get(f"/download/{bid}", headers={"Range": "bytes=0-9"})
    assert client.get("/api/builds").json()["latest"]["downloads"] == 3


def test_build_contents_lists_zip(tmp_path, monkeypatch):
    client, admin, _ = client_with_downloads(tmp_path, monkeypatch)
    bid = upload(client, admin["token"], make_zip()).json()["id"]

    contents = client.get(f"/api/builds/{bid}/contents")
    assert contents.status_code == 200
    body = contents.json()
    assert body["total_files"] == 2
    paths = {e["path"] for e in body["entries"]}
    assert paths == {"Burnout5.exe", "asset.dat"}
    assert body["total_size"] == len(b"exe") + len(b"asset bytes")

    # unknown build -> 404
    assert client.get("/api/builds/999/contents").status_code == 404


def test_latest_reflects_newest_and_prunes_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_KEEP_BUILDS", "2")
    client, admin, _ = client_with_downloads(tmp_path, monkeypatch)

    ids = []
    for i in range(4):
        # distinct content -> distinct content-addressed filename per build
        resp = upload(client, admin["token"], make_zip(f"exe-{i}".encode()), commit=str(i) * 40)
        assert resp.status_code == 201
        ids.append(resp.json()["id"])

    # only the newest 2 remain listed and on disk
    listing = client.get("/api/builds").json()
    assert [b["id"] for b in listing["builds"]] == [ids[3], ids[2]]

    downloads = tmp_path / "downloads"
    zips = list(downloads.glob("burnout-*.zip"))
    assert len(zips) == 2

    # the newest still downloads; a pruned one is gone
    assert client.get(f"/download/{ids[3]}").status_code == 200
    assert client.get(f"/download/{ids[0]}").status_code == 404
