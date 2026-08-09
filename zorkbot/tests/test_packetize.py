from zorkbot.packetize import packetize, strip_ansi


def test_strip_ansi_removes_color_codes() -> None:
    text = "\x1b[37;1mWest of House\x1b[0m"
    assert strip_ansi(text) == "West of House"


def test_packetize_plan_example() -> None:
    text = (
        "You are in a forest.  There is a large tree here.  "
        "A small path leads north."
    )
    # Full text is ~74 chars; use a tighter limit to match the planning doc example.
    packets = packetize(text, max_chars=70)
    assert len(packets) == 2
    assert packets[0].startswith("(1/2) You are in a forest.")
    assert packets[0].endswith("A small path")
    assert packets[1] == "(2/2) leads north."
    assert all(len(packet) <= 70 for packet in packets)


def test_packetize_fits_single_packet_at_default_limit() -> None:
    text = (
        "You are in a forest.  There is a large tree here.  "
        "A small path leads north."
    )
    packets = packetize(text, max_chars=100)
    assert packets == [
        "You are in a forest. There is a large tree here. A small path leads north."
    ]


def test_packetize_single_packet_omits_sequence_numbers() -> None:
    packets = packetize("Taken.", max_chars=100)
    assert packets == ["Taken."]


def test_packetize_prefix_is_budgeted() -> None:
    text = "word " * 30
    prefix = "@[player] "
    packets = packetize(text, max_chars=40, prefix=prefix, numbered=False)
    assert all(packet.startswith(prefix) for packet in packets)
    assert all(len(packet) <= 40 for packet in packets)


def test_packetize_strips_ansi_before_splitting() -> None:
    text = "\x1b[31mRed\x1b[0m " + ("blue " * 20)
    packets = packetize(text, max_chars=30, numbered=False)
    assert all("\x1b" not in packet for packet in packets)


def test_packetize_collapses_blank_lines() -> None:
    text = "Line one.\n\n\nLine two."
    packets = packetize(text, max_chars=100, numbered=False)
    assert packets == ["Line one. Line two."]


def test_packetize_empty_input() -> None:
    assert packetize("   \n\n  ") == []
