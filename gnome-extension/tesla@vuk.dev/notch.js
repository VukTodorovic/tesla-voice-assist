// The notch: a black panel that drops down from the top bar while Tesla is active.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const COMPACT_WIDTH = 260;
const WIDE_WIDTH = 540;
const RESPONSE_MAX_CHARS = 700;
const SHOWN_MAX_CHARS = 1500;
// How long the last answer stays visible after Tesla goes idle.
const LINGER_MS = 6000;
const LINGER_SHOWN_MS = 20000;
const MAX_SOURCES = 6;
const LINGER_EMPTY_MS = 500;

const STATUS = {
    idle: 'Tesla',
    listening: 'Slušam…',
    transcribing: 'Prepoznajem…',
    thinking: 'Razmišljam…',
    speaking: 'Tesla',
    approval: 'Treba mi tvoja dozvola',
};

function wrappingLabel(styleClass) {
    const label = new St.Label({style_class: styleClass, x_expand: true, visible: false});
    label.clutter_text.line_wrap = true;
    label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    return label;
}

// St.BoxLayout picks its size request mode at construction, before `vertical`
// applies, so boxes holding wrapping labels must ask for height-for-width
// explicitly or they get sized as if every label were a single line.
const HEIGHT_FOR_WIDTH = Clutter.RequestMode.HEIGHT_FOR_WIDTH;

// Same header the daemon uses to split off the sources list (text.py).
const SOURCES_HEADER = /^[ \t*#]*(sources|izvori|izvor|reference|linkovi)\**[ \t]*:[\s\S]*$/im;

/** What the response label shows: prose only; code, links and sources get their own widgets. */
function displayText(text) {
    return text.replace(/```[\s\S]*?(```|$)/g, '\n')
        .replace(SOURCES_HEADER, '')
        .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '$1')
        .replace(/https?:\/\/(www\.)?([^/\s)]+)[^\s)]*/g, '$2')
        .replace(/`([^`\n]*)`/g, '$1')
        .split('\n').map(line => line.trim()).filter(Boolean).join('\n');
}

export class Notch {
    constructor({onCancel, onApprove}) {
        this._onCancel = onCancel;
        this._onApprove = onApprove;
        this._state = 'idle';
        this._response = '';
        this._shownCount = 0;
        this._approvalId = 0;
        this._holding = false;
        this._hidden = true;
        this._holds = 0;
        this._wantShow = false;
        this._destroyed = false;
        this._lingerId = 0;
        this._dismiss = false;
        this._targetWidth = this._scaled(COMPACT_WIDTH);

        this.actor = new St.BoxLayout({
            style_class: 'tesla-notch', vertical: true, reactive: true, visible: false,
            width: this._targetWidth, request_mode: HEIGHT_FOR_WIDTH,
        });
        this.actor.connect('button-release-event', () => {
            if (this._state === 'idle')
                this.hide();
            return Clutter.EVENT_PROPAGATE;
        });

        const header = new St.BoxLayout({style_class: 'tesla-header'});
        this._dot = new St.Widget({style_class: 'tesla-dot', y_align: Clutter.ActorAlign.CENTER});
        this._bars = new St.BoxLayout({style_class: 'tesla-bars', y_align: Clutter.ActorAlign.CENTER});
        this._barWidgets = [0.55, 0.85, 1.0, 0.8, 0.6].map(weight => {
            const bar = new St.Widget({style_class: 'tesla-bar', y_align: Clutter.ActorAlign.CENTER});
            bar._weight = weight;
            this._bars.add_child(bar);
            return bar;
        });
        this._status = new St.Label({
            style_class: 'tesla-status', x_expand: true, y_align: Clutter.ActorAlign.CENTER,
        });
        const close = new St.Button({
            style_class: 'tesla-icon-button',
            child: new St.Icon({icon_name: 'window-close-symbolic', icon_size: 14}),
        });
        close.connect('clicked', () => {
            this._onCancel();
            this.hide();
        });
        for (const a of [this._dot, this._bars, this._status, close])
            header.add_child(a);

        this._transcript = wrappingLabel('tesla-transcript');
        this._responseLabel = wrappingLabel('tesla-response');
        this._error = wrappingLabel('tesla-error');
        this._shownBox = new St.BoxLayout({
            style_class: 'tesla-shown-box', vertical: true, visible: false, request_mode: HEIGHT_FOR_WIDTH,
        });

        this._approvalBox = new St.BoxLayout({
            style_class: 'tesla-approval', vertical: true, visible: false, request_mode: HEIGHT_FOR_WIDTH,
        });
        this._approvalText = wrappingLabel('tesla-approval-text');
        this._approvalText.visible = true;
        const buttons = new St.BoxLayout({style_class: 'tesla-approval-buttons', x_align: Clutter.ActorAlign.END});
        const deny = new St.Button({style_class: 'tesla-button tesla-deny', label: 'Odbij'});
        const allow = new St.Button({style_class: 'tesla-button tesla-allow', label: 'Dozvoli'});
        deny.connect('clicked', () => this._answerApproval(false));
        allow.connect('clicked', () => this._answerApproval(true));
        buttons.add_child(deny);
        buttons.add_child(allow);
        this._approvalBox.add_child(this._approvalText);
        this._approvalBox.add_child(buttons);

        this._sourcesBox = new St.BoxLayout({
            style_class: 'tesla-sources', vertical: true, visible: false, request_mode: HEIGHT_FOR_WIDTH,
        });

        for (const a of [header, this._transcript, this._responseLabel, this._shownBox,
            this._sourcesBox, this._approvalBox, this._error])
            this.actor.add_child(a);

        Main.layoutManager.addTopChrome(this.actor);
        this.actor.connect('notify::width', () => this._reposition());
        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._reposition());
        this.setState('idle');
    }

    destroy() {
        this._destroyed = true;
        this._cancelLinger();
        Main.layoutManager.disconnect(this._monitorsId);
        this.actor.destroy();
    }

    // ----- updates from the daemon -----

    setState(state) {
        const previous = this._state;
        this._state = state;
        this._dot.style_class = `tesla-dot tesla-dot-${state}`;
        this._bars.visible = state === 'listening';
        this._dot.visible = state !== 'listening';
        this._updateStatus();
        this._pulse(state === 'thinking' || state === 'transcribing');
        if (state === 'listening' && previous === 'idle') {
            this._clear();
            this._dismiss = false;
        }
        if (state === 'idle' && this._dismiss) {
            this._dismiss = false;
            this._linger(LINGER_EMPTY_MS);
        } else if (state === 'idle') {
            this._linger(this._shownCount || this._sourcesBox.visible ? LINGER_SHOWN_MS
                : this._response || this._error.visible ? LINGER_MS : LINGER_EMPTY_MS);
        } else {
            this.show();
        }
        this._updateWidth();
    }

    /** The conversation was closed ("hvala, ćao"): hide as soon as Tesla goes idle. */
    dismissOnIdle() {
        this._dismiss = true;
    }

    setHolding(holding) {
        this._holding = holding;
        this._updateStatus();
    }

    setLevel(level) {
        if (this._state !== 'listening')
            return;
        for (const bar of this._barWidgets)
            bar.height = this._scaled(Math.round(4 + level * 18 * bar._weight));
    }

    setTranscript(text) {
        // A follow-up question starts a new exchange in the same notch.
        this._response = '';
        this._responseLabel.visible = false;
        this._clearShown();
        this._transcript.text = text ? `„${text}“` : '';
        this._transcript.visible = Boolean(text);
        this._updateWidth();
    }

    appendResponse(delta) {
        this._response += delta;
        let display = displayText(this._response);
        if (display.length > RESPONSE_MAX_CHARS)
            display = `…${display.slice(-RESPONSE_MAX_CHARS)}`;
        this._responseLabel.text = display;
        this._responseLabel.visible = display.length > 0;
        this._updateWidth();
    }

    setActivity(label) {
        if (this._state === 'thinking')
            this._status.text = `${label}…`;
    }

    addShown(text) {
        const row = new St.BoxLayout({style_class: 'tesla-shown', request_mode: HEIGHT_FOR_WIDTH});
        const label = wrappingLabel('tesla-shown-text');
        label.text = text.length > SHOWN_MAX_CHARS ? `${text.slice(0, SHOWN_MAX_CHARS)}\n…` : text;
        label.visible = true;
        const icon = new St.Icon({icon_name: 'edit-copy-symbolic', icon_size: 14});
        const copy = new St.Button({style_class: 'tesla-icon-button', child: icon, y_align: Clutter.ActorAlign.START});
        copy.connect('clicked', () => {
            St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
            icon.icon_name = 'object-select-symbolic';
        });
        row.add_child(label);
        row.add_child(copy);
        this._shownBox.add_child(row);
        this._shownBox.visible = true;
        this._shownCount++;
        this._updateWidth();
    }

    /** Numbered, clickable link cards; Vuk can also say "otvori drugi link". */
    setSources(links) {
        this._sourcesBox.destroy_all_children();
        const shown = links.slice(0, MAX_SOURCES);
        if (!shown.length) {
            this._sourcesBox.visible = false;
            return;
        }
        this._sourcesBox.add_child(new St.Label({
            style_class: 'tesla-sources-header',
            text: shown.length > 1 ? 'IZVORI  ·  „otvori drugi link“' : 'IZVOR  ·  „otvori link“',
        }));
        shown.forEach((link, i) => {
            const row = new St.BoxLayout({style_class: 'tesla-source-row', x_expand: true});
            const badge = new St.Bin({
                style_class: 'tesla-source-num', y_align: Clutter.ActorAlign.CENTER,
                child: new St.Label({
                    text: String(i + 1), x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER,
                }),
            });
            const text = new St.BoxLayout({vertical: true, x_expand: true, y_align: Clutter.ActorAlign.CENTER});
            text.add_child(new St.Label({style_class: 'tesla-source-title', text: link.title}));
            text.add_child(new St.Label({style_class: 'tesla-source-domain', text: link.domain}));
            const icon = new St.Icon({
                style_class: 'tesla-source-icon', icon_name: 'web-browser-symbolic', icon_size: 14,
                y_align: Clutter.ActorAlign.CENTER,
            });
            row.add_child(badge);
            row.add_child(text);
            row.add_child(icon);
            const button = new St.Button({style_class: 'tesla-source', x_expand: true, child: row});
            button.connect('clicked', () => {
                Gio.AppInfo.launch_default_for_uri(link.url, global.create_app_launch_context(0, -1));
                button.add_style_class_name('tesla-source-opened');
            });
            this._sourcesBox.add_child(button);
        });
        this._sourcesBox.visible = true;
        this._updateWidth();
    }

    showApproval(id, description) {
        this._approvalId = id;
        this._approvalText.text = description;
        this._approvalBox.visible = true;
        this.actor.add_style_class_name('tesla-needs-approval');
        this.show();
        this._updateWidth();
    }

    hideApproval(id) {
        if (id !== this._approvalId)
            return;
        this._approvalId = 0;
        this._approvalBox.visible = false;
        this.actor.remove_style_class_name('tesla-needs-approval');
        this._updateWidth();
    }

    setError(message) {
        this._error.text = message;
        this._error.visible = Boolean(message);
        this._updateWidth();
    }

    /** Briefly show a message when the daemon is not reachable. */
    flash(message) {
        this._clear();
        this.setError(message);
        this.show();
        this._linger(4000);
    }

    // ----- visibility -----

    /**
     * Keep the notch out of a screenshot. Returns a release function (safe to call
     * more than once); overlapping holds are counted. While held, a shown notch is
     * blanked and show() is deferred. On the last release the notch comes back in
     * whatever state it should be in now, so a capture never strands it invisible
     * halfway through its fade-in (which saving and restoring opacity used to do).
     */
    holdForCapture() {
        this._holds++;
        if (this._holds === 1 && !this._hidden)
            this.actor.visible = false;
        let released = false;
        return () => {
            if (released || this._destroyed)
                return;
            released = true;
            if (--this._holds > 0)
                return;
            if (!this._hidden) {
                this.actor.remove_all_transitions();
                this.actor.opacity = 255;
                this.actor.translation_y = 0;
                this.actor.visible = true;
                this._reposition();
            } else if (this._wantShow) {
                this._wantShow = false;
                this.show();
            }
        };
    }

    show() {
        this._cancelLinger();
        if (this._holds > 0) {
            this._wantShow = true;
            return;
        }
        if (!this._hidden)
            return;
        this._hidden = false;
        this.actor.remove_all_transitions();
        this.actor.visible = true;
        this.actor.opacity = 0;
        this.actor.translation_y = -this._scaled(18);
        this._reposition();
        this.actor.ease({
            opacity: 255, translation_y: 0, duration: 180, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
    }

    hide() {
        this._cancelLinger();
        this._wantShow = false;
        if (this._hidden)
            return;
        this._hidden = true;
        this.actor.ease({
            opacity: 0, translation_y: -this._scaled(18), duration: 160,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => {
                if (this._hidden)
                    this.actor.visible = false;
            },
        });
    }

    // ----- internals -----

    _answerApproval(allow) {
        if (this._approvalId)
            this._onApprove(this._approvalId, allow);
        this.hideApproval(this._approvalId);
    }

    _updateStatus() {
        if (this._state === 'listening' && this._holding)
            this._status.text = 'Slušam dok držiš…';
        else
            this._status.text = STATUS[this._state] ?? this._state;
    }

    _clear() {
        this._response = '';
        for (const label of [this._transcript, this._responseLabel, this._error]) {
            label.text = '';
            label.visible = false;
        }
        this._clearShown();
        this._holding = false;
    }

    _clearShown() {
        this._shownBox.destroy_all_children();
        this._shownBox.visible = false;
        this._shownCount = 0;
        this._sourcesBox.destroy_all_children();
        this._sourcesBox.visible = false;
    }

    _pulse(on) {
        this._dot.remove_all_transitions();
        this._dot.opacity = 255;
        if (on) {
            this._dot.ease({
                opacity: 60, duration: 650, mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                repeatCount: -1, autoReverse: true,
            });
        }
    }

    _updateWidth() {
        const wide = this._transcript.visible || this._responseLabel.visible ||
            this._shownCount > 0 || this._sourcesBox.visible || this._approvalBox.visible ||
            this._error.visible;
        const width = this._scaled(wide ? WIDE_WIDTH : COMPACT_WIDTH);
        if (width !== this._targetWidth) {
            this._targetWidth = width;
            this.actor.ease({width, duration: 200, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        }
    }

    _reposition() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor)
            return;
        const top = monitor.y + (Main.panel.visible ? Main.panel.height : 0);
        this.actor.set_position(Math.round(monitor.x + (monitor.width - this.actor.width) / 2), top);
    }

    _linger(ms) {
        this._cancelLinger();
        this._lingerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._lingerId = 0;
            this.hide();
            return GLib.SOURCE_REMOVE;
        });
    }

    _cancelLinger() {
        if (this._lingerId) {
            GLib.source_remove(this._lingerId);
            this._lingerId = 0;
        }
    }

    _scaled(px) {
        return px * St.ThemeContext.get_for_stage(global.stage).scale_factor;
    }
}
