// Tesla: notch UI and screen context for the local voice assistant daemon.
//
// The GNOME custom shortcut calls dev.vuk.TeslaShell.Activate. On activation we
// capture what Vuk is looking at (focused window, fresh selection, a screenshot
// of that monitor), hand it to the daemon, and watch the held modifiers for
// hold-to-talk. Daemon `Event` signals drive the notch.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {Notch} from './notch.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot');
Gio._promisify(Shell.Screenshot.prototype, 'screenshot_area');

const BUS_NAME = 'dev.vuk.Tesla';
const OBJECT_PATH = '/dev/vuk/Tesla';
const SHELL_BUS_NAME = 'dev.vuk.TeslaShell';
const SHELL_PATH = '/dev/vuk/TeslaShell';

// Holding the shortcut's modifiers this long switches to push-to-talk.
const HOLD_MS = 350;
const HOLD_MODS = Clutter.ModifierType.MOD4_MASK | Clutter.ModifierType.SUPER_MASK |
    Clutter.ModifierType.MOD1_MASK | Clutter.ModifierType.CONTROL_MASK |
    Clutter.ModifierType.SHIFT_MASK;
const SELECTION_TIMEOUT_MS = 150;
const SELECTION_MAX_CHARS = 4000;
const SCREENSHOT_TIMEOUT_MS = 400;

const TeslaIface = `
<node>
  <interface name="dev.vuk.Tesla">
    <method name="Activate"><arg type="a{ss}" direction="in" name="context"/></method>
    <method name="StopListening"/>
    <method name="SetHold"><arg type="b" direction="in" name="hold"/></method>
    <method name="Cancel"/>
    <method name="Approve">
      <arg type="u" direction="in" name="id"/>
      <arg type="b" direction="in" name="allow"/>
    </method>
    <method name="GetState"><arg type="s" direction="out" name="state"/></method>
    <method name="GetUiSettings"><arg type="a{ss}" direction="out" name="settings"/></method>
    <signal name="Event"><arg type="s" name="kind"/><arg type="s" name="value"/></signal>
  </interface>
</node>`;

const ShellIface = `
<node>
  <interface name="dev.vuk.TeslaShell">
    <method name="Activate"/>
    <method name="Screenshot"><arg type="s" direction="out" name="path"/></method>
    <!-- Woken by voice: the same context a hotkey press captures. -->
    <method name="CaptureContext"><arg type="a{ss}" direction="out" name="context"/></method>
    <!-- Development: capture the whole stage, notch included. -->
    <method name="DebugScreenshot"><arg type="s" direction="in" name="path"/></method>
  </interface>
</node>`;

const TeslaProxy = Gio.DBusProxy.makeProxyWrapper(TeslaIface);

export default class TeslaExtension extends Extension {
    enable() {
        this._runtimeDir = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'tesla']);
        GLib.mkdir_with_parents(this._runtimeDir, 0o700);
        this._state = 'idle';
        this._selectionChangedAt = 0;
        this._holdPollId = 0;
        this._holding = false;
        // Which screen screenshots show; the daemon's [screen] capture setting.
        this._captureMode = 'pointer';

        this._notch = new Notch({
            onCancel: () => this._call('Cancel'),
            onApprove: (id, allow) => this._call('Approve', id, allow),
        });

        this._proxy = new TeslaProxy(Gio.DBus.session, BUS_NAME, OBJECT_PATH, (_proxy, error) => {
            if (error)
                console.warn(`Tesla: ${error.message}`);
            else
                this._syncState();
        }, null, Gio.DBusProxyFlags.DO_NOT_AUTO_START);
        this._eventId = this._proxy.connectSignal('Event',
            (_proxy, _sender, [kind, value]) => this._onEvent(kind, value));
        this._ownerId = this._proxy.connect('notify::g-name-owner', () => this._syncState());

        this._selection = global.display.get_selection();
        this._selectionId = this._selection.connect('owner-changed', (_sel, type) => {
            if (type === Meta.SelectionType.SELECTION_PRIMARY)
                this._selectionChangedAt = GLib.get_monotonic_time();
        });

        this._dbusImpl = Gio.DBusExportedObject.wrapJSObject(ShellIface, this);
        this._dbusImpl.export(Gio.DBus.session, SHELL_PATH);
        this._nameId = Gio.DBus.session.own_name(SHELL_BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            () => {}, () => console.warn(`Tesla: lost ${SHELL_BUS_NAME}`));
    }

    disable() {
        this._stopHoldWatch();
        Gio.DBus.session.unown_name(this._nameId);
        this._dbusImpl.unexport();
        this._dbusImpl = null;
        this._selection.disconnect(this._selectionId);
        this._selection = null;
        this._proxy.disconnectSignal(this._eventId);
        this._proxy.disconnect(this._ownerId);
        this._proxy = null;
        this._notch.destroy();
        this._notch = null;
    }

    // ----- D-Bus methods (dev.vuk.TeslaShell) -----

    Activate() {
        this._activate();
    }

    async ScreenshotAsync(_params, invocation) {
        const path = GLib.build_filenamev([this._runtimeDir, 'now.png']);
        // Keep the notch itself out of the picture.
        const release = this._notch.holdForCapture();
        try {
            await this._screenshot(path, global.display.focus_window);
            invocation.return_value(new GLib.Variant('(s)', [path]));
        } catch (e) {
            invocation.return_error_literal(Gio.DBusError, Gio.DBusError.FAILED, String(e));
        } finally {
            release();
        }
    }

    async CaptureContextAsync(_params, invocation) {
        const pressedAt = GLib.get_monotonic_time();
        const window = global.display.focus_window;
        const shot = GLib.build_filenamev([this._runtimeDir, 'press.png']);
        const context = {
            window_title: window?.get_title() ?? '',
            window_class: window?.get_wm_class() ?? '',
            screenshot: shot,
            selection: '',
            selection_age: '1e9',
        };
        // The notch is usually showing by now. Rather than blink it away for the
        // capture, tell the daemon where it is so it can be painted over.
        try {
            const monitor = await this._screenshot(shot, window);
            context.mask = this._notchMask(monitor);
        } catch (e) {
            console.warn(`Tesla: screenshot failed: ${e}`);
            context.screenshot = '';
        }
        const text = await new Promise(resolve => this._readSelection(resolve));
        if (text && this._selectionChangedAt) {
            context.selection = text.slice(0, SELECTION_MAX_CHARS);
            context.selection_age = String((pressedAt - this._selectionChangedAt) / 1e6);
        }
        invocation.return_value(new GLib.Variant('(a{ss})', [context]));
    }

    async DebugScreenshotAsync([path], invocation) {
        try {
            const file = Gio.File.new_for_path(path);
            const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
            try {
                await new Shell.Screenshot().screenshot(false, stream);
            } finally {
                stream.close(null);
            }
            invocation.return_value(null);
        } catch (e) {
            invocation.return_error_literal(Gio.DBusError, Gio.DBusError.FAILED, String(e));
        }
    }

    // ----- activation -----

    _activate() {
        if (!this._proxy?.g_name_owner) {
            this._notch.flash('Tesla nije pokrenuta. Pokreni: tesla run');
            return;
        }
        // Second press while listening ends the recording; the daemon handles it.
        if (this._state === 'listening' || this._state === 'approval') {
            this._call('Activate', {});
            return;
        }

        const pressedAt = GLib.get_monotonic_time();
        const mods = global.get_pointer()[2] & HOLD_MODS;
        const window = global.display.focus_window;
        const shot = GLib.build_filenamev([this._runtimeDir, 'press.png']);
        GLib.unlink(shot);
        const context = {
            window_title: window?.get_title() ?? '',
            window_class: window?.get_wm_class() ?? '',
            screenshot: shot,
            selection: '',
            selection_age: '1e9',
        };

        // Screenshot first, notch after, so the notch is never in the picture.
        const release = this._notch.holdForCapture();
        // Never keep the notch hidden for long, even if the screenshot hangs.
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, SCREENSHOT_TIMEOUT_MS, () => {
            release();
            return GLib.SOURCE_REMOVE;
        });
        this._screenshot(shot, window)
            .catch(e => console.warn(`Tesla: screenshot failed: ${e}`))
            .finally(release);

        this._readSelection(text => {
            if (text && this._selectionChangedAt) {
                context.selection = text.slice(0, SELECTION_MAX_CHARS);
                context.selection_age = String((pressedAt - this._selectionChangedAt) / 1e6);
            }
            this._call('Activate', context);
        });

        if (mods)
            this._watchHold(pressedAt, mods);
    }

    _watchHold(pressedAt, mods) {
        this._stopHoldWatch();
        this._holdPollId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 40, () => {
            const held = (global.get_pointer()[2] & mods) === mods;
            if (held) {
                const elapsedMs = (GLib.get_monotonic_time() - pressedAt) / 1000;
                if (!this._holding && elapsedMs >= HOLD_MS) {
                    this._holding = true;
                    this._notch.setHolding(true);
                    this._call('SetHold', true);
                }
                return GLib.SOURCE_CONTINUE;
            }
            if (this._holding) {
                this._holding = false;
                this._notch.setHolding(false);
                this._call('StopListening');
            }
            this._holdPollId = 0;
            return GLib.SOURCE_REMOVE;
        });
    }

    _stopHoldWatch() {
        if (this._holdPollId) {
            GLib.source_remove(this._holdPollId);
            this._holdPollId = 0;
        }
        this._holding = false;
    }

    _readSelection(callback) {
        let done = false;
        const finish = text => {
            if (done)
                return;
            done = true;
            callback(text ?? '');
        };
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, SELECTION_TIMEOUT_MS, () => {
            finish('');
            return GLib.SOURCE_REMOVE;
        });
        St.Clipboard.get_default().get_text(St.ClipboardType.PRIMARY, (_clip, text) => finish(text));
    }

    /** The screen area to capture: {x, y, width, height}, per the capture mode. */
    _captureArea(window) {
        const lm = Main.layoutManager;
        switch (this._captureMode) {
        case 'primary':
            return lm.primaryMonitor;
        case 'focused':
            return lm.monitors[window ? window.get_monitor() : -1] ?? lm.primaryMonitor;
        case 'all': {
            const x = Math.min(...lm.monitors.map(m => m.x));
            const y = Math.min(...lm.monitors.map(m => m.y));
            const right = Math.max(...lm.monitors.map(m => m.x + m.width));
            const bottom = Math.max(...lm.monitors.map(m => m.y + m.height));
            return {x, y, width: right - x, height: bottom - y};
        }
        default: { // 'pointer'
            const [px, py] = global.get_pointer();
            return lm.monitors.find(m => px >= m.x && px < m.x + m.width &&
                py >= m.y && py < m.y + m.height) ?? lm.primaryMonitor;
        }
        }
    }

    async _screenshot(path, window) {
        const area = this._captureArea(window);
        const tmp = Gio.File.new_for_path(`${path}.tmp`);
        const stream = tmp.replace(null, false, Gio.FileCreateFlags.NONE, null);
        try {
            const shooter = new Shell.Screenshot();
            await shooter.screenshot_area(area.x, area.y, area.width, area.height, stream);
        } finally {
            stream.close(null);
        }
        tmp.move(Gio.File.new_for_path(path), Gio.FileCopyFlags.OVERWRITE, null, null);
        return area;
    }

    /** "x,y,w,h,area_w,area_h" of the notch (with its shadow) within the captured area, or "". */
    _notchMask(monitor) {
        const actor = this._notch.actor;
        if (!actor.visible)
            return '';
        const [x, y] = actor.get_transformed_position();
        const [w, h] = actor.get_transformed_size();
        // Shadow, and the slide-in offset if it is still animating.
        const pad = 28 * St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const box = [x - monitor.x - pad, y - monitor.y - pad, w + 2 * pad, h + 2 * pad];
        if (box[0] + box[2] < 0 || box[1] + box[3] < 0 || box[0] > monitor.width || box[1] > monitor.height)
            return '';
        return [...box, monitor.width, monitor.height].map(v => Math.round(v)).join(',');
    }

    // ----- daemon -> notch -----

    _onEvent(kind, value) {
        switch (kind) {
        case 'state':
            this._state = value;
            if (value !== 'listening')
                this._stopHoldWatch();
            this._notch.setState(value);
            break;
        case 'level':
            this._notch.setLevel(parseFloat(value));
            break;
        case 'transcript':
            this._notch.setTranscript(value);
            break;
        case 'delta':
            this._notch.appendResponse(value);
            break;
        case 'tool':
            this._notch.setActivity(value);
            break;
        case 'shown':
            this._notch.addShown(value);
            break;
        case 'sources':
            this._notch.setSources(JSON.parse(value));
            break;
        case 'approval': {
            const {id, description} = JSON.parse(value);
            this._notch.showApproval(id, description);
            break;
        }
        case 'approval_done':
            this._notch.hideApproval(parseInt(value));
            break;
        case 'error':
            this._notch.setError(value);
            break;
        case 'dismiss':
            this._notch.dismissOnIdle();
            break;
        }
    }

    _syncState() {
        if (!this._proxy?.g_name_owner) {
            this._state = 'idle';
            this._notch.setState('idle');
            return;
        }
        this._proxy.GetStateRemote((result, error) => {
            if (!error && this._notch) {
                this._state = result[0];
                this._notch.setState(result[0]);
            }
        });
        this._proxy.GetUiSettingsRemote((result, error) => {
            if (!error && result[0].capture)
                this._captureMode = result[0].capture;
        });
    }

    _call(method, ...args) {
        this._proxy?.[`${method}Remote`](...args, (_result, error) => {
            if (error)
                console.warn(`Tesla: ${method} failed: ${error.message}`);
        });
    }
}
