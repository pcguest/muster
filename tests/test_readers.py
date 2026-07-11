"""Chunked reading: bounded chunks must be indistinguishable from one read."""

import polars as pl
import pytest

from muster.readers import ROW_COLUMN, ReaderError, iter_table_chunks, read_table


def _write_csv(path, rows):
    path.write_text("id,name\n" + "".join(f"{i},row{i}\n" for i in range(rows)))


def test_csv_chunks_are_bounded_and_row_numbers_continuous(tmp_path):
    path = tmp_path / "big.csv"
    _write_csv(path, 10)
    chunks = list(iter_table_chunks(path, chunk_rows=4))
    assert [c.height for c in chunks] == [4, 4, 2]
    numbers = [n for c in chunks for n in c.get_column(ROW_COLUMN).to_list()]
    assert numbers == list(range(2, 12))  # header is row 1
    values = [v for c in chunks for v in c.get_column("id").to_list()]
    assert values == [str(i) for i in range(10)]


def test_chunked_read_equals_whole_read_for_xlsx(tmp_path):
    path = tmp_path / "book.xlsx"
    pl.DataFrame(
        {"id": [str(i) for i in range(9)], "amount": [f"{i}.5" for i in range(9)]}
    ).write_excel(path)
    whole = read_table(path)
    chunked = pl.concat(iter_table_chunks(path, chunk_rows=4), how="vertical")
    assert chunked.equals(whole)
    assert [c.height for c in iter_table_chunks(path, chunk_rows=4)] == [4, 4, 1]


def test_header_only_file_still_yields_its_columns(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("id,name\n")
    chunks = list(iter_table_chunks(path, chunk_rows=4))
    assert len(chunks) == 1
    assert chunks[0].height == 0
    assert set(chunks[0].columns) == {ROW_COLUMN, "id", "name"}


def test_unreadable_file_raises_reader_error(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    with pytest.raises(ReaderError):
        list(iter_table_chunks(path, chunk_rows=4))


def test_reserved_row_column_is_rejected(tmp_path):
    path = tmp_path / "clash.csv"
    path.write_text("_row,name\n1,a\n")
    with pytest.raises(ReaderError, match="reserved"):
        list(iter_table_chunks(path, chunk_rows=4))
