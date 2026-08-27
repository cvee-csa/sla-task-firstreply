"""update_pending()/update_personal_tasks() are the two functions this whole
app relies on to never lose a write when the cron script and the review
server's own request threads touch the same file at close to the same
moment -- see _atomic_write_json's docstring and the fcntl.flock in
_pending_lock()/_personal_tasks_lock(). This is a real concurrency path
(ThreadingHTTPServer serves every request on its own thread), not just a
theoretical one, so it's worth actually exercising with real concurrent
writers rather than trusting the design by inspection alone.
"""
import threading

import _common


def test_update_pending_survives_many_concurrent_writers():
    """20 threads each add their own distinct key to the pending store at
    close to the same time. Without the lock+atomic-write combination, a
    classic read-modify-write race would lose some of these -- two threads
    both read the same starting dict, both add their own key in memory, and
    whichever writes last wins, silently discarding the other's key. If the
    locking is doing its job, every single one of the 20 keys survives."""
    barrier = threading.Barrier(20)

    def _add_one(n):
        barrier.wait()  # line every thread up so the writes actually overlap
        _common.update_pending(lambda data: data.__setitem__(f"ticket_{n}", {"n": n}))

    threads = [threading.Thread(target=_add_one, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = _common.load_pending()
    assert len(data) == 20
    assert set(data.keys()) == {f"ticket_{n}" for n in range(20)}


def test_update_pending_mutate_fn_receives_the_latest_data_each_time():
    """A second, more direct check on the same guarantee: an increment
    repeated many times from many threads must land on the correct final
    total, not just "some number of keys, roughly right." A lost update
    here would show up as a wrong count, not a crash."""
    _common.update_pending(lambda data: data.__setitem__("counter", {"value": 0}))

    def _increment():
        for _ in range(25):
            def _mutate(data):
                data["counter"]["value"] += 1
            _common.update_pending(_mutate)

    threads = [threading.Thread(target=_increment) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _common.load_pending()["counter"]["value"] == 8 * 25


def test_update_personal_tasks_survives_many_concurrent_writers():
    """Same guarantee, for the personal-tasks store's own lock/atomic-write
    pair (a separate lock and a separate file from the pending store)."""
    barrier = threading.Barrier(15)

    def _add_one(n):
        barrier.wait()
        _common.update_personal_tasks(lambda data: data.__setitem__(f"task_{n}", {"n": n}))

    threads = [threading.Thread(target=_add_one, args=(n,)) for n in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = _common.load_personal_tasks()
    assert len(data) == 15
    assert set(data.keys()) == {f"task_{n}" for n in range(15)}


def test_atomic_write_leaves_no_partial_file_on_disk():
    """_atomic_write_json writes to a sibling .tmp file and os.replace()s it
    over the real path -- the real path should never be observable as
    anything other than complete, valid JSON, even immediately after a
    write. This doesn't (and can't, from a single-process test) simulate a
    genuine crash mid-write, but it does confirm the real target file is
    only ever touched by the atomic replace, never partially written to
    directly."""
    _common.update_pending(lambda data: data.__setitem__("a", {"x": 1}))
    _common.update_pending(lambda data: data.__setitem__("b", {"x": 2}))

    # The file on disk must parse as complete JSON matching the last write --
    # not truncated, not a half-written intermediate state.
    on_disk = _common.load_pending()
    assert on_disk == {"a": {"x": 1}, "b": {"x": 2}}
