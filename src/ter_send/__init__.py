import argparse
from pathlib import Path
import subprocess
from typing import override
import sys

import cantools
from cantools.database.can.database import Database
from cantools.database.can.message import Message
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Select

CAN_IF = "can0"

class MessageListScreen(Screen[None]):
    def __init__(self, db: Database, dbc_name: str) -> None:
        super().__init__()
        self.db: Database = db
        self.dbc_name: str = dbc_name

    @override
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(classes="container"):
            with Vertical(classes="panel"):
                yield Label(f"📂 Loaded DBC: {self.dbc_name}", classes="title")
                yield Input(placeholder="Search messages by name or ID...", id="search_input")
                yield ListView(id="message_list")
        yield Footer()

    async def on_mount(self) -> None:
        await self._populate_list("")
        _ = self.query_one("#message_list", ListView).focus()

    @on(Input.Changed, "#search_input")
    async def _on_search_changed(self, event: Input.Changed) -> None:
        await self._populate_list(event.value)

    async def _populate_list(self, filter_text: str) -> None:
        list_view = self.query_one("#message_list", ListView)

        await list_view.clear()

        search = filter_text.lower()
        items: list[ListItem] = []

        for msg in sorted(self.db.messages, key=lambda m: m.name):
            msg_id_hex = f"0x{msg.frame_id:X}".lower()
            msg_name = msg.name.lower()

            if search in msg_name or search in msg_id_hex or search in str(msg.frame_id):
                items.append(
                    ListItem(
                        Label(f"{msg.name} (ID: 0x{msg.frame_id:X})"),
                        id=f"msg_{msg.frame_id}",
                    )
                )

        await list_view.extend(items)

    @on(ListView.Selected, "#message_list")
    def _on_message_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id and item_id.startswith("msg_"):
            frame_id = int(item_id.removeprefix("msg_"))
            selected_message = self.db.get_message_by_frame_id(frame_id)
            _ = self.app.push_screen(MessageBuilderScreen(selected_message))


class MessageBuilderScreen(Screen[None]):
    BINDINGS = [("escape", "app.pop_screen", "Back to Message List")]

    def __init__(self, message: Message) -> None:
        super().__init__()
        self.message = message

    @override
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Container(classes="container"):
            with Vertical(classes="detail-view"):
                yield Label(f"🛠️ Build Message: {self.message.name}", classes="title")
                yield Label(f"ID: 0x{self.message.frame_id:X} | Length: {self.message.length} bytes")

                with ScrollableContainer(classes="signal-container"):
                    for sig in self.message.signals:
                        range_str = f"[{sig.minimum} to {sig.maximum}]" if sig.minimum is not None else ""
                        unit_str = f" {sig.unit}" if sig.unit else ""
                        mux_indicator = " (MUX)" if sig.is_multiplexer else ""

                        with Horizontal(id=f"row_{sig.name}", classes="signal-row"):
                            yield Label(
                                f"{sig.name}{mux_indicator} {range_str}{unit_str}:",
                                classes="signal-label",
                            )

                            if sig.choices:
                                options = [(f"{k}: {v}", k) for k, v in sig.choices.items()]
                                default_val = options[0][1] if options else Select.BLANK
                                yield Select(
                                    options,
                                    value=default_val,
                                    id=f"sig_{sig.name}",
                                    allow_blank=False,
                                    classes="signal-select",
                                )
                            else:
                                yield Input(
                                    placeholder="0",
                                    id=f"sig_{sig.name}",
                                    classes="signal-input",
                                )

                with Horizontal(classes="action-row"):
                    yield Button("Send (Append to Log)", id="send_btn", variant="success")
                    yield Button("Cancel", id="cancel_btn", variant="error")

                yield Label("", id="output_log", classes="output-log")

        yield Footer()

    @override
    def on_mount(self) -> None:
        self._update_visibility()

        for widget in self.query("Input, Select"):
            if widget.display:
                widget.focus()
                break

    @on(Select.Changed)
    @on(Input.Changed)
    def _on_input_changed(self) -> None:
        self._update_visibility()

    def _get_signal_value(self, sig_name: str) -> int | float:
        try:
            widget = self.query_one(f"#sig_{sig_name}")
            if isinstance(widget, Select):
                return int(widget.value) if widget.value != Select.BLANK else 0
            if isinstance(widget, Input):
                val = widget.value.strip()
                return float(val) if val else 0.0
        except (NoMatches, ValueError):
            pass
        return 0.0

    def _update_visibility(self) -> None:
        for sig in self.message.signals:
            if not sig.multiplexer_ids or not sig.multiplexer_signal:
                continue

            parent_val = int(self._get_signal_value(sig.multiplexer_signal))

            try:
                row = self.query_one(f"#row_{sig.name}", Horizontal)
                row.display = parent_val in sig.multiplexer_ids
            except NoMatches:
                pass

    @on(Button.Pressed, "#cancel_btn")
    def _on_cancel(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#send_btn")
    def _on_send(self) -> None:
        output_label = self.query_one("#output_log", Label)
        signal_data: dict[str, int | float] = {}

        for sig in self.message.signals:
            try:
                row = self.query_one(f"#row_{sig.name}", Horizontal)
                if row.display:
                    signal_data[sig.name] = self._get_signal_value(sig.name)
            except NoMatches:
                continue

        warning = None
        try:
            payload = self.message.encode(signal_data, strict=True)
        except Exception as e:
            warning = str(e)
            try:
                payload = self.message.encode(signal_data, strict=False)
            except Exception as e2:
                output_label.update(f"❌ Error packing frame: {e2}")
                return

        hex_payload = payload.hex().upper()
        frame_id_hex = f"{self.message.frame_id:03X}"
        cansend_cmd = f"cansend {CAN_IF} {frame_id_hex}#{hex_payload}"

        try:
            subprocess.run(
                ["cansend", CAN_IF, f"{frame_id_hex}#{hex_payload}"],
                check=True,
                capture_output=True,
                text=True
            )
        except FileNotFoundError:
            output_label.update("❌ 'cansend' not found. Is can-utils installed and on PATH?")
            return
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.strip() if e.stderr else str(e)
            output_label.update(f"❌ Transmission error: {error_msg}")
            return

        if warning:
            output_label.update(f"✅ Sent: {cansend_cmd}\nWith warning: {warning}")
        else:
            output_label.update(f"✅ Sent: {cansend_cmd}")

class CanSerializerApp(App[None]):
    TITLE = "TER Send"

    CSS = """
    Screen { background: #1e1e2e; color: #cdd6f4; }
    Header, Footer { background: #313244; color: #89b4fa; }
    .container { padding: 2 4; align: center middle; height: 100%; }
    .panel { width: 80%; height: 80%; border: heavy #89b4fa; background: #181825; padding: 1 2; }
    .title { text-style: bold; color: #f9e2af; margin-bottom: 1; }

    #search_input { margin-bottom: 1; background: #11111b; border: tall #45475a; }
    #search_input:focus { border: tall #a6e3a1; }

    ListView { background: #11111b; border: solid #45475a; height: 1fr; margin-bottom: 1; }
    ListItem { padding: 1; }
    ListItem:hover { background: #313244; }

    .detail-view { padding: 2; background: #181825; border: heavy #a6e3a1; width: 90%; height: 90%; }
    .signal-container { margin-top: 1; border: solid #45475a; height: 1fr; background: #11111b; padding: 1; }
    .signal-row { height: auto; margin-bottom: 1; align: left middle; }
    .signal-label { width: 40%; content-align: right middle; padding-right: 1; }
    .signal-input, .signal-select { width: 60%; }

    .action-row { height: auto; margin-top: 1; align: center middle; }

    Button { margin: 0 1; background: #89b4fa; color: #11111b; }
    Button:focus { background: #a6e3a1; border: tall #94e2d5; color: #11111b; text-style: bold; }
    Button:hover { background: #b4befe; }

    Input:focus, Select:focus { border: tall #a6e3a1; }

    .output-log { margin-top: 1; text-align: left; color: #a6e3a1; text-style: bold; }
    """

    def __init__(self, dbc_path: Path) -> None:
        super().__init__()
        self.dbc_path = dbc_path
        try:
            self.db = cantools.database.load_file(str(self.dbc_path))
        except FileNotFoundError:
            raise SystemExit(f"❌ DBC file not found: {self.dbc_path}")
        except Exception as e:
            raise SystemExit(f"❌ Failed to load DBC '{self.dbc_path}': {e}")

    @override
    def on_mount(self) -> None:
        self.push_screen(MessageListScreen(self.db, self.dbc_path.name))


def main() -> None:
    parser = argparse.ArgumentParser(description="CAN DBC Builder Terminal App")
    parser.add_argument("dbc_path", type=Path, help="Path to the DBC database file")
    parser.add_argument("--can", type=str, help="CAN interface name (e.g., can0, vcan0)")
    parser.add_argument("--decode", action="store_true", help="Launch cantools monitor instead of the sender app")
    args = parser.parse_args()

    if args.can:
        global CAN_IF
        CAN_IF = args.can

    if args.decode:
        sys.argv = [
            "cantools",
            "monitor",
            "-c", CAN_IF,
            "-i", "socketcan",
            str(args.dbc_path)
        ]

        from cantools import __main__ as cantools_main

        if hasattr(cantools_main, '_main'):
            cantools_main._main()
        else:
            cantools_main.main()

        sys.exit(0)

    app = CanSerializerApp(args.dbc_path)
    app.run()


if __name__ == "__main__":
    main()
