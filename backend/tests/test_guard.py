import pytest

from app.assistant.guard import PRICE_LINE, PROMISE_LINE, StreamGuard, check_sentence


@pytest.mark.parametrize("sentence", [
    "A typical migration costs $5,000.",
    "Managed IT starts at £45 per user.",
    "It is around 2k USD a month.",
    "Our day rate is 900 euros.",
    "Support is billed at 120 per hour.",
    "The assessment is EUR 3.5k.",
    "Budget about 1.2 million dollars.",
])
def test_prices_are_replaced(sentence):
    v = check_sentence(sentence, [])
    assert v.reason == "price" and v.text == PRICE_LINE


@pytest.mark.parametrize("sentence", [
    "We have about 50 people.",
    "Discovery calls are free and take 30 minutes.",
    "Office hours are 9:00 to 18:00 UK time.",
    "We help with ISO 27001 and Cyber Essentials.",
    "Companies of 10 employees and up.",
    "Your budget of a few thousand is noted.",
])
def test_ordinary_numbers_pass(sentence):
    assert check_sentence(sentence, []).reason is None


def test_an_approved_price_can_be_quoted():
    assert check_sentence("The health check is £1,500.", ["£1500"]).reason is None
    assert check_sentence("The health check is £1,900.", ["£1500"]).reason == "price"


@pytest.mark.parametrize("sentence", [
    "We guarantee 99.9% uptime.",
    "Your migration is guaranteed to finish in a week.",
    "I promise we will be done by Friday.",
    "We offer 100% uptime on managed servers.",
])
def test_promises_are_replaced(sentence):
    v = check_sentence(sentence, [])
    assert v.reason == "promise" and v.text == PROMISE_LINE


@pytest.mark.parametrize("sentence", [
    "I can't guarantee a start date here.",
    "We cannot promise timelines in chat.",
    "No outcome is guaranteed before a proposal.",
])
def test_declining_to_promise_is_fine(sentence):
    assert check_sentence(sentence, []).reason is None


def test_stream_checks_whole_sentences_even_when_split_across_pieces():
    g = StreamGuard([])
    out = ""
    for piece in ["We do cloud migra", "tions. A typical one is $", "8,000 for a small ", "office. Want a call?"]:
        out += g.feed(piece)
    out += g.flush()
    assert "$" not in out and "8,000" not in out
    assert out.startswith("We do cloud migrations. ")
    assert PRICE_LINE in out and out.endswith("Want a call?")
    assert [v.reason for v in g.blocked] == ["price"]


def test_safe_line_is_said_once_per_reply():
    g = StreamGuard([])
    out = g.feed("Option A is $100. Option B is $200. ") + g.flush()
    assert out.count(PRICE_LINE) == 1 and len(g.blocked) == 2


def test_nothing_is_released_before_the_sentence_ends():
    g = StreamGuard([])
    assert g.feed("The cost is $") == ""
    assert g.feed("400") == ""
    assert g.flush() == PRICE_LINE


def test_no_promise_to_follow_up_later():
    from app.assistant.guard import FOLLOWUP_LINE, check_sentence
    for s in ["I'll let you know promptly when there's any progress.", "We will keep you posted.",
              "I’ll notify you as soon as it moves.", "I will get back to you tomorrow."]:
        assert check_sentence(s, []).text == FOLLOWUP_LINE, s
    for s in ["Your consultant will contact you if anything is needed.", "You can ask me here any time.",
              "I can't let you know about other clients."]:
        assert check_sentence(s, []).reason is None, s


def test_machine_start_values_never_reach_the_visitor():
    """Seen live: the model copied the tool's start values into the slot list it offered."""
    g = StreamGuard([])
    out = g.feed("Here are a few options:\n* Friday 20 November at 14:00 Karachi time "
                 "(start 2026‑11‑20T14:00:00+05:00)\n* Friday at 14:30 (start=2026-11-20T09:30:00Z)\n"
                 "Which time works best? ") + g.flush()
    assert "2026" not in out and "start" not in out
    assert "* Friday 20 November at 14:00 Karachi time\n* Friday at 14:30\n" in out
    assert not g.blocked
