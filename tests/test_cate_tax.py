"""标准品类表和查询词展开（common/cate_tax.py，构建时内联进 CODE_MERGE）。"""
import re

import pytest

from common.cate_tax import MAJORS, MINOR_OF, query_map, query_res

QM = query_map()


def test_minors_belong_to_known_majors():
    assert set(MINOR_OF.values()) <= set(MAJORS)


def test_no_category_matches_everything():
    assert query_res("", QM) == ("^", "^", "^", "1", "0")


def test_major_expands_to_synonyms():
    cate_re, name_re, _, dish_w, kb_w = query_res("火锅", QM)
    assert re.search(cate_re, "|火锅|")
    for w in ["潮汕牛肉火锅", "椰子鸡", "串串"]:
        assert re.search(name_re, w)
    assert (dish_w, kb_w) == ("1", "0")


def test_unknown_word_falls_back_to_substring():
    cate_re, name_re, dish_re, dish_w, kb_w = query_res("猪手", QM)
    assert cate_re == "a^" and re.search(name_re, "隆江猪脚饭猪手") and kb_w == "1"


@pytest.mark.parametrize("kw", ["西餐", "日料", "粤菜", "甜品", "咖啡", "快餐", "pasta"])
def test_common_words_hit_taxonomy(kw):
    assert query_res(kw, QM)[0] != "a^"


def test_inlined_source_is_self_contained():
    # CODE_MERGE 内联的是 query_res 的源码，只能依赖 re
    import inspect
    src = inspect.getsource(query_res)
    ns = {"re": re}
    exec(src, ns)
    assert ns["query_res"]("火锅", QM) == query_res("火锅", QM)
