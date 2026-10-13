from voice_agent.text_chunker import TextChunker


def make(**kwargs):
    flushed = []
    return TextChunker(on_flush=flushed.append, **kwargs), flushed


def test_flushes_a_chunk_when_punctuation_arrives_split_across_multiple_pushes():
    chunker, flushed = make()
    chunker.push('Xin ')
    chunker.push('chào')
    chunker.push('.')
    chunker.push(' Bạn')
    assert flushed == ['Xin chào.']


def test_flushes_multiple_punctuation_bounded_chunks_from_one_push():
    chunker, flushed = make()
    chunker.push('Chào bạn, hôm nay trời đẹp. Bạn khỏe không?')
    assert flushed == ['Chào bạn,', 'hôm nay trời đẹp.', 'Bạn khỏe không?']


def test_falls_back_to_a_word_count_flush_when_no_punctuation_appears():
    chunker, flushed = make(max_words_before_flush=4)
    chunker.push('một hai ba bốn năm sáu')
    assert flushed == ['một hai ba bốn']


def test_word_count_flush_keeps_the_space_before_the_next_token():
    chunker, flushed = make(max_words_before_flush=2)
    chunker.push('một hai ba ')  # trailing space belongs between 'ba' and the next token
    chunker.push('bốn')
    chunker.end()
    assert flushed == ['một hai', 'ba bốn']


def test_end_flushes_any_remaining_buffered_text():
    chunker, flushed = make()
    chunker.push('không có dấu câu')
    chunker.end()
    assert flushed == ['không có dấu câu']


def test_end_does_not_flush_an_empty_or_whitespace_only_buffer():
    chunker, flushed = make()
    chunker.push('   ')
    chunker.end()
    assert flushed == []


def test_does_not_hang_when_max_words_before_flush_is_0_regression():
    chunker, flushed = make(max_words_before_flush=0)
    chunker.push('một hai ba')
    # Clamped to 1: flushes word by word, keeping the last word buffered.
    assert flushed == ['một', 'hai']


def test_absorbs_back_to_back_punctuation_into_single_chunk():
    chunker, flushed = make()
    chunker.push('Hello?!')
    assert flushed == ['Hello?!']


def test_absorbs_back_to_back_punctuation_followed_by_trailing_text_into_a_single_chunk():
    chunker, flushed = make()
    chunker.push('Hello?! Bạn khỏe không?')
    assert flushed == ['Hello?!', 'Bạn khỏe không?']


def test_does_not_split_a_decimal_number_at_the_punctuation_boundary():
    chunker, flushed = make()
    chunker.push('Giá khoảng 8.5 triệu đồng.')
    assert flushed == ['Giá khoảng 8.5 triệu đồng.']


def test_does_not_split_a_decimal_number_even_when_more_punctuation_bearing_text_follows():
    chunker, flushed = make()
    chunker.push('Giá 8.5 triệu, đã bao gồm thuế.')
    assert flushed == ['Giá 8.5 triệu,', 'đã bao gồm thuế.']
