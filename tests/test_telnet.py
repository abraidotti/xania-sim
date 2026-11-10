from xania_sim.core.telnet import DO, DONT, IAC, SB, SE, WILL, WONT, strip_telnet

TTYPE, NAWS, ECHO = 24, 31, 1


def test_refuses_every_option_and_keeps_text():
    pending = bytearray()
    text, replies = strip_telnet(bytes([IAC, DO, TTYPE, IAC, DO, NAWS, IAC, WILL, ECHO]) + b"Name: ", pending)
    assert text == "Name: "
    assert replies == [bytes([IAC, WONT, TTYPE]), bytes([IAC, WONT, NAWS]), bytes([IAC, DONT, ECHO])]
    assert not pending


def test_wont_and_dont_need_no_reply():
    text, replies = strip_telnet(bytes([IAC, WONT, ECHO, IAC, DONT, ECHO]) + b"ok", bytearray())
    assert (text, replies) == ("ok", [])


def test_sequences_split_across_reads_are_kept_for_the_next():
    pending = bytearray()
    assert strip_telnet(b"ab" + bytes([IAC]), pending) == ("ab", [])
    assert pending == bytes([IAC])
    assert strip_telnet(bytes([DO]), pending) == ("", [])
    text, replies = strip_telnet(bytes([TTYPE]) + b"cd", pending)
    assert (text, replies) == ("cd", [bytes([IAC, WONT, TTYPE])])


def test_subnegotiation_escaped_iac_ansi_and_crlf():
    data = bytes([IAC, SB, TTYPE, 1, IAC, SE]) + b"x" + bytes([IAC, IAC]) + b"\x1b[1;31mred\x1b[0m\n\r"
    text, _ = strip_telnet(data, bytearray())
    assert text == "x\xffred\n"
