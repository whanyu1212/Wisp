from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import pytest

from wisp.events import ImagePart
from wisp.images import ImageRejectedError
from wisp.messages import Message
from wisp.sessions import images as images_module
from wisp.sessions.images import SessionImageError, SessionImageStore
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "images"
PNG = (_FIXTURES / "pixel.png").read_bytes()
JPEG = (_FIXTURES / "pixel.jpg").read_bytes()


def _image_message(*images: ImagePart) -> Message:
    return Message(role="user", content="what is this?", images=images)


def test_image_survives_reopen(tmp_path: Path) -> None:
    """Store, append, reopen and read back, in the order a real run does it."""

    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def attach() -> tuple[ImagePart, ImagePart]:
        png = await session.store_image(PNG)
        jpeg = await session.store_image(JPEG)
        await session.append_message(_image_message(png, jpeg))
        return png, jpeg

    png, jpeg = anyio.run(attach)
    reopened = store.load(session.session_id)

    (message,) = reopened.read_messages()
    assert message.images == (png, jpeg)
    assert anyio.run(reopened.read_image, png) == PNG
    assert anyio.run(reopened.read_image, jpeg) == JPEG


def test_same_image_stored_once(tmp_path: Path) -> None:
    images = SessionImageStore(tmp_path / "images")

    first = images.store(PNG)
    inode = (images.directory / first.sha256).stat().st_ino
    second = images.store(PNG)

    assert first == second
    assert [path.name for path in images.directory.iterdir()] == [first.sha256]
    assert (images.directory / first.sha256).stat().st_ino == inode


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_image_files_are_private(tmp_path: Path) -> None:
    images = SessionImageStore(tmp_path / "images")

    image = images.store(PNG)

    assert stat.S_IMODE(images.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((images.directory / image.sha256).stat().st_mode) == 0o600


def test_invalid_image_is_not_stored(tmp_path: Path) -> None:
    images = SessionImageStore(tmp_path / "images")

    with pytest.raises(ImageRejectedError):
        images.store(PNG[:-12])

    assert not images.directory.exists()


def test_append_needs_stored_image(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    unstored = SessionImageStore(tmp_path / "elsewhere").store(PNG)

    with pytest.raises(SessionImageError, match=unstored.sha256):
        anyio.run(session.append_message, _image_message(unstored))

    assert not session.path.exists()


def test_tampered_image_fails_read(tmp_path: Path) -> None:
    images = SessionImageStore(tmp_path / "images")
    image = images.store(PNG)
    path = images.directory / image.sha256
    path.write_bytes(PNG[:-1] + b"\x00")

    with pytest.raises(SessionImageError, match="does not match its digest"):
        images.read(image)


def test_store_repairs_tampered_image(tmp_path: Path) -> None:
    images = SessionImageStore(tmp_path / "images")
    image = images.store(PNG)
    (images.directory / image.sha256).write_bytes(PNG[:-1] + b"\x00")

    images.store(PNG)

    assert images.read(image) == PNG


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_symlinked_image_is_refused(tmp_path: Path) -> None:
    images = SessionImageStore(tmp_path / "images")
    image = images.store(PNG)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    path = images.directory / image.sha256
    path.unlink()
    path.symlink_to(outside)

    assert not images.contains(image)
    with pytest.raises(SessionImageError):
        images.read(image)


def test_missing_image_fails_read(tmp_path: Path) -> None:
    images = SessionImageStore(tmp_path / "images")
    image = images.store(PNG)
    (images.directory / image.sha256).unlink()

    with pytest.raises(SessionImageError, match="is missing"):
        images.read(image)


def _seed_with_image(session: JsonlSession) -> tuple[ImagePart, str]:
    async def seed() -> tuple[ImagePart, str]:
        image = await session.store_image(PNG)
        await session.append_message(_image_message(image))
        active = await session.append_message(Message(role="assistant", content="a square"))
        return image, active.id

    return anyio.run(seed)


def test_clone_and_fork_share_images(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = store.create()
    image, active_id = _seed_with_image(source)

    async def clone_and_fork() -> tuple[JsonlSession, JsonlSession]:
        clone = await store.clone(source, expected_active_leaf_id=active_id)
        # A fork drops the selected user message, so fork from a later one.
        follow_up = await source.append_message(Message(role="user", content="and now?"))
        forked = await store.fork_from_user_message(
            source, follow_up.id, expected_active_leaf_id=follow_up.id
        )
        return clone, forked.session

    clone, forked = anyio.run(clone_and_fork)

    for copy in (clone, forked):
        assert copy.read_messages()[0].images == (image,)
        assert anyio.run(copy.read_image, image) == PNG
    assert [path.name for path in source.images.directory.iterdir()] == [image.sha256]


def test_clone_copies_images_into_store(tmp_path: Path) -> None:
    """A session opened by path from another directory brings its images along."""

    outside = JsonlSessionStore(tmp_path / "outside").create()
    image, active_id = _seed_with_image(outside)
    store = JsonlSessionStore(tmp_path / "store")

    source = store.load(outside.path)
    clone = anyio.run(lambda: store.clone(source, expected_active_leaf_id=active_id))

    assert clone.images.directory == tmp_path / "store" / "images"
    assert anyio.run(clone.read_image, image) == PNG


def test_messages_without_images_unchanged(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    anyio.run(session.append_message, Message(role="user", content="plain"))

    (line,) = session.path.read_text(encoding="utf-8").splitlines()
    assert "images" not in json.loads(line)["message"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_image_fails_fast(tmp_path: Path) -> None:
    images = SessionImageStore(tmp_path / "images")
    image = images.store(PNG)
    path = images.directory / image.sha256
    path.unlink()
    os.mkfifo(path)

    with pytest.raises(SessionImageError, match="not a regular file"):
        images.read(image)


def test_truncated_image_blocks_append(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()
    image = anyio.run(session.store_image, PNG)
    (session.images.directory / image.sha256).write_bytes(PNG[:-1])

    with pytest.raises(SessionImageError, match="not stored"):
        anyio.run(session.append_message, _image_message(image))


def test_repair_replaces_hard_link(tmp_path: Path) -> None:
    """Repairing a stored image never writes through to another link of its inode."""

    images = SessionImageStore(tmp_path / "images")
    image = images.store(PNG)
    path = images.directory / image.sha256
    path.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"not an image")
    os.link(elsewhere, path)

    images.store(PNG)

    assert images.read(image) == PNG
    assert elsewhere.read_bytes() == b"not an image"


def test_concurrent_stores_agree(tmp_path: Path) -> None:
    images = SessionImageStore(tmp_path / "images")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: images.store(PNG), range(16)))

    assert len(set(results)) == 1
    assert images.read(results[0]) == PNG
    assert [path.name for path in images.directory.iterdir()] == [results[0].sha256]


def test_store_syncs_new_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first image also makes the new ``images`` entry in its parent durable."""

    synced: list[Path] = []
    monkeypatch.setattr(images_module, "sync_directory", synced.append)
    images = SessionImageStore(tmp_path / "images")

    images.store(PNG)
    first_synced = list(synced)
    synced.clear()
    images.store(JPEG)

    assert first_synced == [tmp_path, images.directory]
    assert synced == [images.directory]
