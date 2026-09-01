from zorkbot.packetize import _looks_like_title, packetize, strip_ansi


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


def test_packetize_preserves_line_break_after_room_title() -> None:
    text = "North of House\nYou are facing the north side of a white house."
    packets = packetize(text, max_chars=100, numbered=False)
    assert packets == ["North of House\nYou are facing the north side of a white house."]


def test_packetize_recognizes_title_with_connector_word() -> None:
    text = "Behind the House\nYou are behind the white house."
    packets = packetize(text, max_chars=100, numbered=False)
    assert packets[0].startswith("Behind the House\n")


def test_packetize_title_survives_multi_packet_split() -> None:
    text = (
        "North of House\n"
        "You are facing the north side of a white house. There is no door "
        "here, and all the windows are boarded up. To the north a narrow "
        "path winds through the trees."
    )
    packets = packetize(text, max_chars=100)
    assert len(packets) > 1
    assert packets[0].startswith("North of House\n(1/")
    assert "\n" not in packets[1]


def test_packetize_does_not_treat_sentence_as_title() -> None:
    """A one-line reply like "Taken." ends with a period, so it must never
    be split onto its own line — only a title-shaped, period-less first
    line followed by more text should trigger that."""
    packets = packetize("Taken.", max_chars=100, numbered=False)
    assert packets == ["Taken."]


def test_packetize_does_not_treat_long_sentence_as_title() -> None:
    """The room description itself (not the title) is a full sentence with
    plenty of lowercase non-connector words — it must stay collapsed onto
    one line rather than being misread as a second "title"."""
    text = (
        "You are standing in an open field west of a white house, with a "
        "boarded front door.\nThere is a small mailbox here."
    )
    packets = packetize(text, max_chars=200, numbered=False)
    assert packets == [
        "You are standing in an open field west of a white house, with a "
        "boarded front door. There is a small mailbox here."
    ]


def test_packetize_title_only_response() -> None:
    packets = packetize("North of House", max_chars=100, numbered=False)
    assert packets == ["North of House"]


def test_packetize_prefix_and_title_both_applied_to_first_packet() -> None:
    text = "North of House\nYou are facing the north side of a white house."
    packets = packetize(text, max_chars=100, prefix="@[player] ", numbered=False)
    assert packets == [
        "@[player] North of House\nYou are facing the north side of a white house."
    ]


def test_looks_like_title_examples() -> None:
    assert _looks_like_title("North of House")
    assert _looks_like_title("Behind the House")
    assert _looks_like_title("Dam Lobby")
    assert _looks_like_title("Attic")


def test_looks_like_title_rejects_sentences() -> None:
    assert not _looks_like_title("Taken.")
    assert not _looks_like_title("Ok.")
    assert not _looks_like_title(
        "You are standing in an open field west of a white house"
    )


def test_looks_like_title_rejects_lowercase_first_word() -> None:
    assert not _looks_like_title("the House")


def test_looks_like_title_rejects_long_lines() -> None:
    assert not _looks_like_title("One Two Three Four Five Six Seven")


def test_looks_like_title_rejects_empty() -> None:
    assert not _looks_like_title("")


def test_packetize_first_line_precedes_detected_title() -> None:
    """A watcher's "[Name] > command" echo must not swallow the room title
    detection, which only ever looks at the start of `text` itself."""
    text = "North of House\nYou are facing the north side of a white house."
    packets = packetize(
        text, max_chars=120, first_line="[Alice] > north", numbered=False
    )
    assert packets == [
        "[Alice] > north\nNorth of House\nYou are facing the north side of a white house."
    ]


def test_packetize_first_line_with_non_title_body() -> None:
    packets = packetize(
        "Taken.", max_chars=120, first_line="[Alice] > take lamp", numbered=False
    )
    assert packets == ["[Alice] > take lamp\nTaken."]


def test_packetize_first_line_appears_once_across_multiple_packets() -> None:
    text = (
        "North of House\n"
        "You are facing the north side of a white house. There is no door "
        "here, and all the windows are boarded up. To the north a narrow "
        "path winds through the trees."
    )
    packets = packetize(text, max_chars=100, first_line="[Alice] > north")
    assert len(packets) > 1
    assert packets[0].startswith("[Alice] > north\nNorth of House\n(1/")
    assert "[Alice]" not in packets[1]
    assert "\n" not in packets[1]


def test_packetize_first_line_alone_with_no_body() -> None:
    packets = packetize("", max_chars=100, first_line="[Alice] > look")
    assert packets == ["[Alice] > look"]


def test_packetize_no_first_line_is_unaffected() -> None:
    packets = packetize("Taken.", max_chars=100, numbered=False)
    assert packets == ["Taken."]
