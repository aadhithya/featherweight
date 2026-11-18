from micro.store import RunStore


async def test_set_get_roundtrip():
    store = RunStore()
    await store.set("job-1", "extracted", {"sections": ["a", "b"], "n": 2})
    assert await store.get("job-1", "extracted") == {"sections": ["a", "b"], "n": 2}
    await store.close()


async def test_missing_key_is_none():
    store = RunStore()
    assert await store.get("job-1", "nope") is None
    await store.close()


async def test_delete():
    store = RunStore()
    await store.set("job-1", "k", [1, 2, 3])
    await store.delete("job-1", "k")
    assert await store.get("job-1", "k") is None
    await store.close()


async def test_keys_are_scoped_per_job():
    store = RunStore()
    await store.set("job-1", "k", "one")
    await store.set("job-2", "k", "two")
    assert await store.get("job-1", "k") == "one"
    assert await store.get("job-2", "k") == "two"
    await store.close()


async def test_instances_do_not_share_data():
    a, b = RunStore(), RunStore()
    await a.set("job-1", "k", "only in a")
    assert await b.get("job-1", "k") is None
    await a.close()
    await b.close()
