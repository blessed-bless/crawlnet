import time

from crawlnet.frontier import CANCELLED, FINISHED, RUNNING


async def test_dedup_and_limits(frontier, spec):
    spec.settings.max_pages = 3
    job = await frontier.create_job(spec)
    assert await frontier.enqueue(job, "https://shop.test/a", "item", 1)
    assert not await frontier.enqueue(job, "https://shop.test/a", "item", 1)  # дубль
    assert await frontier.enqueue(job, "https://shop.test/b", "item", 1)
    assert not await frontier.enqueue(job, "https://shop.test/c", "item", 1)  # лимит страниц
    j = await frontier.job(job)
    assert (j["enqueued"], j["pending"], j["dropped"]) == (3, 3, 1)


async def test_job_finishes_when_last_request_acked(frontier, spec):
    job = await frontier.create_job(spec)
    req = await frontier.pop()
    assert req.url == "https://shop.test/" and await frontier.status(job) == RUNNING
    await frontier.enqueue(job, "https://shop.test/a", "item", 1)  # потомок до ack родителя
    await frontier.ack(req)
    assert await frontier.status(job) == RUNNING
    await frontier.ack(await frontier.pop())
    assert await frontier.status(job) == FINISHED


async def test_delayed_requests_are_not_popped_early(frontier, spec):
    job = await frontier.create_job(spec)
    req = await frontier.pop()
    await frontier.retry(req, delay=30)
    assert await frontier.pop() is None
    assert 29 < await frontier.next_ready_in() <= 30
    assert (await frontier.job(job))["retries"] == 1


async def test_reaper_returns_lost_requests(frontier, spec, redis):
    await frontier.create_job(spec)
    req = await frontier.pop()
    assert await frontier.pop() is None
    # «воркер умер»: сдвигаем дедлайн в прошлое
    await redis.zadd(frontier.k("inflight"), {req.dumps(): time.time() - 1})
    assert await frontier.reap() == 1
    assert (await frontier.pop()) == req


async def test_double_ack_after_reap_does_not_corrupt_pending(frontier, spec, redis):
    job = await frontier.create_job(spec)
    await frontier.enqueue(job, "https://shop.test/a", "item", 1)
    slow = await frontier.pop()
    await redis.zadd(frontier.k("inflight"), {slow.dumps(): time.time() - 1})
    await frontier.reap()
    fast = await frontier.pop()  # тот же запрос взял второй воркер
    await frontier.ack(fast)
    await frontier.ack(slow)  # медленный воркер всё-таки доделал
    assert (await frontier.job(job))["pending"] == 1


async def test_politeness_slot(frontier):
    assert await frontier.acquire_slot("shop.test", 1.0) == 0
    wait = await frontier.acquire_slot("shop.test", 1.0)
    assert 0 < wait <= 1.0
    assert await frontier.acquire_slot("other.test", 1.0) == 0


async def test_cancel(frontier, spec):
    job = await frontier.create_job(spec)
    assert await frontier.cancel(job)
    assert not await frontier.cancel(job)
    assert not await frontier.enqueue(job, "https://shop.test/z", "item", 1)
    await frontier.drop(await frontier.pop())
    assert await frontier.status(job) == CANCELLED
