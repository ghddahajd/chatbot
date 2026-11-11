from app.utils.jsonl import read_jsonl


def test_read_jsonl_preserves_unicode_in_rows_and_skips_incomplete_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    assert read_jsonl(path) == []

    path.write_bytes(
        ('{"message":"первая\u2028вторая\u0085строка"}\r\n'
         '\r\n{"incomplete":\r\n42\r\n{"last":true}').encode("utf-8")
    )
    assert read_jsonl(path) == [{"message": "первая\u2028вторая\u0085строка"}, {"last": True}]
