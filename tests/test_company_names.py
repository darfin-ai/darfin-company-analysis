from dart_pipeline.company_names import canonical_company_name


def test_sk_hynix_uses_official_display_name() -> None:
    assert canonical_company_name("000660", "에스케이하이닉스") == "SK하이닉스"


def test_other_company_name_is_preserved() -> None:
    assert canonical_company_name("005930", "삼성전자") == "삼성전자"
