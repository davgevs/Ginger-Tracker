"""Standalone Ginger Tracker Test Lab. No production imports, files, or processes."""
import argparse
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path


def clock(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}'


@dataclass
class FakeTracker:
    state: str = 'idle'
    elapsed: float = 0.0
    saved: list = field(default_factory=list)
    peak: float = 0.0
    submissions: int = 0

    def load(self):
        if self.state not in ('idle', 'complete'):
            raise ValueError('Finish the current simulated task first')
        self.state, self.elapsed, self.peak = 'active', 0.0, 0.0

    def advance(self, seconds):
        if self.state not in ('active', 'submitting') or seconds < 0:
            raise ValueError('Cannot advance the simulated timer')
        self.elapsed += seconds
        self.peak = max(self.peak, self.elapsed)

    def submit(self):
        if self.state == 'active':
            self.state = 'submitting'
            self.submissions += 1
        elif self.state != 'submitting':
            raise ValueError('No task to submit')

    def complete(self):
        if self.state != 'submitting':
            raise ValueError('No submitted task')
        self.saved.append(round(self.peak, 1))
        self.state = 'complete'

    @property
    def average(self):
        return sum(self.saved) / len(self.saved) if self.saved else 0


class FakeUpdater:
    """In-memory update/rollback state model; never launches processes."""
    def __init__(self):
        self.installed = '6.1.1'
        self.backup = None

    def install(self, new_version, checksum_ok=True, restart_ok=True):
        if not checksum_ok:
            return False
        self.backup = self.installed
        self.installed = new_version
        if not restart_ok:
            self.installed = self.backup
            return False
        return True


class TestLab(unittest.TestCase):
    def test_submission_duration_keeps_advancing(self):
        t = FakeTracker(); t.load(); t.advance(600); t.submit(); t.advance(180); t.complete()
        self.assertEqual(t.saved, [780])

    def test_duplicate_popup_only_one_completion(self):
        t = FakeTracker(); t.load(); t.advance(120); t.submit(); t.submit(); t.complete()
        with self.assertRaises(ValueError): t.complete()
        self.assertEqual(len(t.saved), 1)

    def test_average_completed_tasks(self):
        t = FakeTracker()
        for seconds in (120, 240):
            t.load(); t.advance(seconds); t.submit(); t.complete()
        self.assertEqual(t.average, 180)

    def test_reset_between_tasks(self):
        t = FakeTracker(); t.load(); t.advance(600); t.submit(); t.complete()
        t.load(); self.assertEqual(t.elapsed, 0); t.advance(60); t.submit(); t.complete()
        self.assertEqual(t.saved, [600, 60])

    def test_reject_completion_without_submission(self):
        t = FakeTracker(); t.load()
        with self.assertRaises(ValueError): t.complete()

    def test_temporary_history_only(self):
        t = FakeTracker(); t.load(); t.advance(200); t.submit(); t.complete()
        with tempfile.TemporaryDirectory(prefix='ginger_testlab_') as folder:
            path = Path(folder) / 'fake_history.json'
            path.write_text(json.dumps({'durations': t.saved}), encoding='utf-8')
            self.assertEqual(json.loads(path.read_text())['durations'], [200])

    def test_updater_checksum_failure(self):
        u = FakeUpdater(); self.assertFalse(u.install('6.1.2', checksum_ok=False))
        self.assertEqual(u.installed, '6.1.1')

    def test_updater_restart_rollback(self):
        u = FakeUpdater(); self.assertFalse(u.install('6.1.2', restart_ok=False))
        self.assertEqual(u.installed, '6.1.1')

    def test_updater_success(self):
        u = FakeUpdater(); self.assertTrue(u.install('6.1.2'))
        self.assertEqual(u.installed, '6.1.2')


def run_tests():
    import io
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TestLab))
    return result, stream.getvalue()


def launch_gui():
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title('Ginger Tracker — Test Lab (Simulation Only)')
    root.geometry('620x620')
    root.minsize(500, 480)
    root.configure(bg='#f3f6fb')
    tracker = FakeTracker()
    outer = ttk.Frame(root, padding=18)
    outer.pack(fill='both', expand=True)
    ttk.Label(outer, text='Ginger Tracker — Test Lab', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
    ttk.Label(outer, text='SIMULATION ONLY • No Ginger connection • No real history writes', foreground='#567').pack(anchor='w', pady=(4, 12))
    status = tk.StringVar()
    timer = tk.StringVar()
    history = tk.StringVar()
    def refresh():
        status.set('State: ' + tracker.state.upper() + '   |   Completed: ' + str(len(tracker.saved)))
        timer.set('Current timer: ' + clock(tracker.elapsed) + '   |   Average: ' + (clock(tracker.average) if tracker.saved else '—'))
        history.set('Saved fake durations: ' + (', '.join(clock(v) for v in tracker.saved[-8:]) or 'none'))
    for var in (status, timer, history):
        ttk.Label(outer, textvariable=var, font=('Segoe UI', 11)).pack(anchor='w', pady=3)
    def action(method, *args):
        try:
            method(*args)
            log.insert('end', f'✓ {method.__name__} {args}\n')
        except ValueError as exc:
            log.insert('end', f'! {exc}\n')
        refresh()
        log.see('end')
    buttons = ttk.Frame(outer)
    buttons.pack(fill='x', pady=12)
    controls = [('Load fake task', tracker.load, ()), ('+1 minute', tracker.advance, (60,)),
                ('+10 minutes', tracker.advance, (600,)), ('Submission popup', tracker.submit, ()),
                ('Confirm completion', tracker.complete, ())]
    for i, (label, method, args) in enumerate(controls):
        ttk.Button(buttons, text=label, command=lambda m=method,a=args: action(m,*a)).grid(row=i//2,column=i%2,sticky='ew',padx=4,pady=4)
    buttons.columnconfigure(0, weight=1); buttons.columnconfigure(1, weight=1)
    ttk.Separator(outer).pack(fill='x', pady=9)
    ttk.Label(outer, text='Automated regression checks', font=('Segoe UI', 12, 'bold')).pack(anchor='w')
    def test_action():
        result, report = run_tests()
        log.insert('end', '\n' + report + '\n')
        log.see('end')
        result_label.set(f'{result.testsRun - len(result.failures) - len(result.errors)}/{result.testsRun} PASS' if result.wasSuccessful() else 'TESTS FAILED')
    result_label = tk.StringVar(value='Not run')
    ttk.Button(outer, text='Run all automated tests', command=test_action).pack(fill='x', pady=(8, 4))
    ttk.Label(outer, textvariable=result_label, font=('Segoe UI', 11, 'bold')).pack(anchor='w')
    log = tk.Text(outer, height=12, wrap='word', font=('Consolas', 9))
    log.pack(fill='both', expand=True, pady=8)
    refresh()
    root.mainloop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Isolated Ginger Tracker simulation lab')
    parser.add_argument('--test', action='store_true', help='Run automated tests without opening GUI')
    args = parser.parse_args()
    if args.test:
        result, report = run_tests()
        print(report)
        raise SystemExit(0 if result.wasSuccessful() else 1)
    launch_gui()
