from crabagent.ontology_route import compile_ontology_route


def test_korean_request_words_are_not_sent_as_ontology_terms() -> None:
    route = compile_ontology_route(
        "오픈크랩 팩을 조회해서 브랜드 전략에 도움이 될 근거를 찾아줘"
    )

    terms = route["query_terms"]
    assert "팩" not in terms
    assert "조회" not in terms
    assert "찾아줘" not in terms
    assert "오픈크랩" in terms
    assert "브랜드" in terms
    assert "전략" in terms
    assert "근거" in terms
