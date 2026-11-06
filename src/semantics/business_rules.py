"""Finite full-clause business policies; recognized by the shared rulebook parser."""

CLAUSES = {
    "lot_expiry": (
        "lo gan het han nhat cua mot thuoc",
        "trong cac lo con ton chua qua han tai hom nay, lo co han som nhat",
    ),
    "quantity_ranking": (
        "ban chay",
        (
            "theo so luong quy ve don vi co ban, khong theo so dong hay so tien; bo "
            "don huy, tra. mat hang khac don vi co ban van xep chung theo so do. ban "
            "nhieu nhat, ban duoc nhieu nhat, hot nhat cung nghia voi ban chay nhat"
        ),
    ),
    "customer_identity": (
        "xac dinh khach",
        (
            "shop co ma khach: moi ma khach la mot khach, hai ma trung ten la hai "
            "khach; don khong ghi ma (khach le) khong phai khach, khong dem va khong "
            "xep hang. shop khong co ma khach: xac dinh khach theo ten ghi tren don: "
            "hai ten chi khac cach viet la cung mot khach, hai ten khac chu hoac khac"
            " so la hai khach; don khach le khong tinh. khach mua nhieu tien nhat xep"
            " theo doanh thu (§1)"
        ),
    ),
    "debt_status": ("don duoc tinh vao no", "don khong huy; don tra hang van tinh neu con no"),
    "debt_balance": (
        "no mot don tai ngay x",
        (
            "phan chua tra cua tong don (so khach phai tra ca don: da tru giam gia "
            "tren ca don, gom phi giao), tinh cac khoan tra den het ngay x. con no = "
            "cong cac don con no > 0"
        ),
    ),
    "debt_customer": (
        "no theo khach",
        (
            "xac dinh khach nhu §3. don khach le van tinh vao tong no, nhung khong co"
            " dong trong bang no theo khach"
        ),
    ),
    "debt_cod": (
        "don cod, don ban qua san tmdt",
        "khong phai khach no, ke ca khi chua thu duoc tien; bo khoi cong no va tuoi no",
    ),
    "debt_collection": (
        "no them, tra no, thu no trong ky",
        (
            "chi don ghi no, khong huy. no them: phan tong don chua tra ngay trong "
            "ngay mua, cua don dat trong ky. tra no / thu no: cac khoan tra sau ngay "
            "mua co ngay tra trong ky, cua moi don ghi no (ke ca don ky truoc, don "
            "tra hang). tien tra cung ngay mua la tra luc mua. hoan tien tra hang "
            "khong tru"
        ),
    ),
    "purchase_amount": (
        "tien nhap hang trong ky",
        (
            "tong tien cac phieu nhap co ngay phieu trong ky, bo phieu huy. phieu, "
            "dong ghi nhap dau ky la phieu nhap nhu moi phieu khac: co ngay phieu "
            "trong ky thi tinh. nha cung cap nhap nhieu nhat = nha cung cap co tien "
            "nhap cao nhat"
        ),
    ),
    "gross_profit": (
        "lai gop",
        (
            "doanh thu (§1) tru gia von cua hang da ban. gia von cua moi dong ban "
            "bang so luong nhan voi gia von ghi tren dong ban (cho mot don vi ban, "
            "cung don vi voi don gia). khong tu tinh lai gia von tu phieu nhap hay tu"
            " lo"
        ),
    ),
    "ranking_ties": (
        "- bang nhau:",
        (
            "hai doi tuong bang nhau o tieu chi cau hoi thi xep theo ma tang dan "
            "(khong co ma thi theo ten). cau hoi mot doi tuong lay doi tuong dung dau"
            " theo thu tu do"
        ),
    ),
}


CLAUSE_PARAMETERS = {
    "lot_expiry": {"remaining": ">0", "expiry": ">=as_of", "select": "earliest"},
    "quantity_ranking": {
        "measure": "base_quantity",
        "statuses": ["completed", "shipping"],
        "mixed_base_units_rank_together": True,
    },
    "customer_identity": {
        "coded_shop": "code_identity; uncoded_orders_excluded",
        "uncoded_shop": "source_name_identity; different_letters_or_digits_distinct",
        "walkins": "excluded",
        "money_ranking": "revenue",
    },
    "debt_status": {"exclude": ["cancelled"], "include_returned_if_positive": True},
    "debt_balance": {
        "basis": "order_payable_after_order_discount_including_shipping",
        "payments": "through_end_cutoff_day",
        "reduce": "sum(max(payable-paid,0)_per_order)",
    },
    "debt_customer": {"anonymous": "included_in_shop_total_excluded_from_customer_table"},
    "debt_cod": {"exclude": ["cod", "marketplace"], "also_exclude_from": "debt_age"},
    "debt_collection": {
        "method": "debt",
        "exclude": ["cancelled"],
        "new_debt": "placed_in_period; payable_minus_same_day_payments",
        "collection": "payment_in_period_after_purchase_day; include_prior_orders_and_returns",
        "refunds": "do_not_subtract",
    },
    "purchase_amount": {
        "grain": "purchase_header",
        "axis": "purchased_at",
        "exclude": ["cancelled"],
        "include_opening_import": True,
        "supplier_ranking": "purchase_money",
    },
    "gross_profit": {
        "formula": "revenue-sum(selling_quantity*line_cost_per_selling_unit)",
        "recompute_from_purchases_or_lots": False,
    },
    "ranking_ties": {"order": "measure_desc_code_asc_else_name", "single": "first"},
}

EXECUTION = {
    "debt_collection": "bounded: collections only; new debt and refund source mapping unavailable",
    "purchase_amount": "bounded: explicit purchase headers only; supplier ranking unavailable",
    "lot_expiry": "bounded: nearest nonexpired remaining lot at declared export end only",
    "customer_identity": "bounded: source identities; customer revenue/order-count ranking",
    "ordered_results": "bounded: at most three scalar results in question order",
    "rounding": "bounded: undefined ties abstain; AOV has explicit half-up policy",
}


# Reviewed full-clause signatures, not source-file hashes or split defaults.
# See docs/rulebook-authority.md for source/line/hash and execution limitations.
# Canonical parameters are independent of the finite wording recognizer.
CATALOGUE = {
    "precedence": {
        "selector": "@precedence",
        "requirements": "supported question conditions; verified scope; quality gates",
        "execution": "bounded",
        "meanings": {
            "572b46bb86f8a1d84aa93d7998490e3c610faa0375f80f0d1f5d8ab701f64d0a": {
                "order": ["explicit_question", "verified_industry", "general"],
                "question_creates_missing_facts": False,
            }
        },
        "scope": "general",
    },
    "as_of_recording": {
        "selector": "@as_of_recording",
        "requirements": "source event date and recorded order status",
        "execution": "bounded",
        "meanings": {
            "a1328eda58632b7d5b4b50bbb5bf67bcfc361e8b0ef9c5d7c7fb44f7fa8a53fa": {
                "anchor": "question_as_of_date",
                "future_events": "excluded_when_asof",
                "returns": "recorded_status_even_if_return_after_asof",
            }
        },
        "scope": "general",
    },
    "revenue_terms": {
        "selector": "ban duoc bao nhieu tien, thu ve bao nhieu tien",
        "requirements": "net merchandise; sales status/order date",
        "execution": "bounded",
        "meanings": {
            "36f512dddbacad35619412faf82bbb41e198d07026f0ec79a3d6947107f7fe22": {
                "metric": "revenue",
                "not": "cash_received",
            }
        },
        "scope": "general",
    },
    "cancelled_status": {
        "selector": "don bi huy",
        "requirements": "recorded status",
        "execution": "bounded",
        "meanings": {
            "3bbd88cb7bee44873e0cf970dfb1b3f3030ee0b5e5b055505d202d6392266d2e": {
                "status": "cancelled",
                "returned_is_cancelled": False,
            }
        },
        "scope": "general",
    },
    "payment_revenue": {
        "selector": "doanh thu theo hinh thuc thanh toan",
        "requirements": "order payment-method mapping; no payment fanout",
        "execution": "unavailable",
        "meanings": {
            "b86a2be3e15e510ce5e571a005aff2cd495fa87849ad3f371e9b5688c7a1ec18": {
                "axis": "ordered_at",
                "method": "recorded_on_order",
                "exclude_debt_from": ["cash", "transfer"],
                "cod_is_cash": False,
            }
        },
        "scope": "general",
    },
    "day_flow": {
        "selector": "ngay x, hom nay, hom qua",
        "requirements": "source-local event date; relative anchor",
        "execution": "bounded",
        "meanings": {
            "b50c66e0db51a84028858a9f88becc8c2f8fd696fae75602e75c8787676fb199": {
                "kind": "flow",
                "bounds": "one_calendar_day",
            }
        },
        "scope": "general",
    },
    "end_day_state": {
        "selector": "den het ngay x, tinh den hom nay",
        "requirements": "complete relevant history or exact authorized snapshot",
        "execution": "bounded",
        "meanings": {
            "f232a867559d3bcaf28c6635bb0bf6bd3ec76a409a57afed0a746a44818eb5dd": {
                "kind": "state",
                "history": "all_prior_events",
                "cutoff": "end_of_day",
                "not": "current_month",
            },
            "329112a8a63f83e41b9d0c832b974325d427efd283ca8e254e8758d176b2d4a3": {
                "kind": "state",
                "history": "all_prior_events",
                "cutoff": "end_of_day",
                "not": "current_month",
            },
        },
        "scope": "general",
    },
    "threshold": {
        "selector": "qua n, hon n, tren n, duoi n",
        "requirements": "typed quantity/age/cost basis and threshold operator",
        "execution": "unavailable",
        "meanings": {
            "7c50b43e5af2508bec012b5f4c18a88bf76756adadf3990c078cae0080dc009f": {
                "over": ">",
                "under": "<",
                "from": ">=",
            }
        },
        "scope": "general",
    },
    "base_unit_total": {
        "selector": "ban duoc bao nhieu <don vi>",
        "requirements": "unique product identity; base quantity; conversion authority",
        "execution": "bounded: one grounded product only",
        "meanings": {
            "bc7f748d2bbd9af4eab0dd9bc6bd1a9b11656597890a679e01592d2b7e161ba9": {
                "sum": "base_quantity",
                "filter": "catalog_base_unit",
                "include_large_unit_sales": True,
            }
        },
        "scope": "general",
    },
    "profit_ranking": {
        "selector": "hang loi nhat",
        "requirements": "selling-unit line cost; product identity",
        "execution": "bounded",
        "meanings": {
            "4bbece3aabcc1a606d68a7f7a75db0a1d8a62de7ae8adc4c67a2ddb52b0248cf": {
                "measure": "total_gross_profit",
                "ref": "gross_profit",
            }
        },
        "scope": "general",
    },
    "staff_revenue": {
        "selector": "nhan vien ban gioi nhat",
        "requirements": "staff identity; order attribution",
        "execution": "unavailable",
        "meanings": {
            "fcfb9c6512f4091fef6177ef8a1784399149feee21a27c42082fb18f1ae6df7e": {
                "measure": "revenue",
                "not": "order_count",
            }
        },
        "scope": "general",
    },
    "debt_age": {
        "selector": "tuoi no",
        "requirements": "debt balance; order date; age operator",
        "execution": "unavailable",
        "meanings": {
            "a5cc9b80b13dd1e9061987f80604030d87c03393c9814f150e1cdab805c33d55": {
                "axis": "ordered_at",
                "formula": "cutoff_day-order_day",
                "today_age": 0,
                "not": "due_date",
            }
        },
        "scope": "general",
    },
    "purchase_sales_quantity": {
        "selector": "nhap tru ban, chenh nhap va ban",
        "requirements": "purchase lines; sold base quantity; source conversions",
        "execution": "unavailable",
        "meanings": {
            "1f73f5a3d1d868bd095a655e87e5277584a5d6ef777fa9497cbc1d3ceb086a46": {
                "sold_statuses": ["completed", "shipping"],
                "unit": "same_as_purchase",
                "formula": "purchased-sold",
            }
        },
        "scope": "general",
    },
    "weighted_purchase_price": {
        "selector": "gia nhap binh quan mot mat hang trong thang",
        "requirements": "purchase lines; month coverage; unit conversion",
        "execution": "unavailable",
        "meanings": {
            "fd6dddd1abd70f409aedb1947088173b05630c66f490d881932b98cc9e1b4943": {
                "axis": "purchased_at",
                "status": "not_cancelled",
                "unit": "purchase_unit",
                "formula": "sum(quantity*price)/sum(quantity)",
            }
        },
        "scope": "general",
    },
    "large_unit_stock": {
        "selector": "ton theo don vi lon",
        "requirements": "stock at cutoff; conversion chain; ordered two-number output",
        "execution": "unavailable",
        "meanings": {
            "aac6292f280cac2e26fe367f5b4036babd0e9066a5523dfae345c9178f81eb26": {
                "output": ["whole_large_units", "remainder_base_units"],
                "intermediate_remainder": False,
                "zero_remainder": 0,
            }
        },
        "scope": "general",
    },
    "stock_valuation": {
        "selector": "gia tri hang ton kho",
        "requirements": "stock and catalog cost at required basis; no lot-cost substitution",
        "execution": "unavailable",
        "meanings": {
            "847dddfbfb063eaa02dba3b7f2e4371895a655d9c00ee10284f5afd95aabe80e": {
                "cutoff": "end_today",
                "quantity_filter": ">0",
                "cost": "catalog_cost_per_base_unit",
            }
        },
        "scope": "general",
    },
    "stockout": {
        "selector": "mat hang het hang cuoi ngay",
        "requirements": "end-day balances; catalog product type; no intraday-only inference",
        "execution": "unavailable",
        "meanings": {
            "3d6b803eea7dd4a63b8ca10bac0943d1182624b14270c9483d022c83ae84f671": {
                "cutoff": "end_day",
                "balance": 0,
                "products": "purchased_for_resale",
                "exclude": "prepared_on_site",
            }
        },
        "scope": "general",
    },
    "dead_stock": {
        "selector": "mat hang khong ban duoc trong ky (e)",
        "requirements": "all prior qualified sales and period completeness",
        "execution": "unavailable",
        "meanings": {
            "bc331602e02192f3c9f3ce62425d8874f3478b900e465a52f2ce22e82c8ec635": {
                "prior_sales": True,
                "period_sales": False,
                "statuses": ["completed", "shipping"],
            }
        },
        "scope": "general",
    },
    "day_sale_price": {
        "selector": "gia ban x ngay d",
        "requirements": "sale-line price/unit conversion; explicit date",
        "execution": "unavailable",
        "meanings": {
            "0df37f0aeb223a65bf363eac11082254bed1df1bd614bd05518753ac8222c36d": {
                "axis": "ordered_at",
                "statuses": "all",
                "basis": "before_line_discount_per_base_unit",
            }
        },
        "scope": "general",
    },
    "price_effect": {
        "selector": "hieu ung tang gia",
        "requirements": "ordered price history; quantity; both windows; conversion",
        "execution": "unavailable",
        "meanings": {
            "7b844dd5360446d8e5f19028052c2a2f7eee293f36d1c2c41cf0d054ab6e4bd0": {
                "increase": "new>previous",
                "repeat_same_new_price": "first",
                "after": "[D,D+N-1]",
                "before": "[D-N,D-1]",
                "statuses": ["completed", "shipping"],
            }
        },
        "scope": "general",
    },
    "generic_cost": {
        "selector": "gia von",
        "requirements": "weighted cost history; not permission to recompute gross-profit line cost",
        "execution": "unavailable",
        "meanings": {
            "3786201007e2906f1bd1886759ceed157f5d58b5962202a7a21db65242874c48": {
                "basis": "weighted_import_lots_at_sale",
                "fifo": False,
                "last_purchase": False,
            }
        },
        "scope": "general",
    },
    "net_profit": {
        "selector": "lai rong mot thang",
        "requirements": "expense ledger; gross profit; monthly coverage",
        "execution": "unavailable",
        "meanings": {
            "c2fae0d36fa79fea35b25b2d28cff7a5e5c54e2d444060e8dbebe7a198d69775": {
                "formula": "monthly_gross_profit-all_monthly_expenses",
                "include": ["repairs", "equipment_replacement"],
            }
        },
        "scope": "general",
    },
    "industry_precedence": {
        "selector": "@industry_precedence",
        "requirements": "README industry declaration or explicit domain description",
        "execution": "bounded",
        "meanings": {
            "db9ad189e4b414b677870053e29d5df3cc1eda9dfc0c63440e085a44169f78ca": {
                "priority": "industry_over_general",
                "requires": "verified_industry",
            }
        },
        "scope": "general",
    },
    "coffee_size": {
        "selector": "size m, size l",
        "requirements": "verified cafe industry; source name suffix",
        "execution": "unavailable",
        "meanings": {
            "5a5a5975790f7ed535a5277c3d1510b12161fa4af58001c3130d08943dad0d88": {
                "match": "name_suffix_(M)_or_(L)",
                "case_sensitive": False,
            }
        },
        "scope": "ca phe",
    },
    "prescription_share": {
        "selector": "thuoc ke don",
        "requirements": "verified pharmacy; drug catalog flag; revenue and denominator",
        "execution": "unavailable",
        "meanings": {
            "d35eb707fc9623f7c2c8af5cc2a58ccdfad60b07265f9de15592dc820e472db3": {
                "prescription": "catalog_flag",
                "others": "nonprescription",
                "measure": "revenue",
            }
        },
        "scope": "nha thuoc",
    },
    "expiry_horizon": {
        "selector": "hang het han trong n ngay toi",
        "requirements": "verified pharmacy; remaining lots; dated returns; lot cost; units",
        "execution": "unavailable",
        "meanings": {
            "722dea6b1b1f235cbf7fa53a967732422489361657d7281bfb89278f4887a70b": {
                "bounds": "[D,D+N]",
                "remaining": ">0",
                "cost": "own_lot_purchase_cost_per_base_unit",
            }
        },
        "scope": "nha thuoc",
    },
    "active_ingredient": {
        "selector": "doanh thu theo hoat chat",
        "requirements": "verified pharmacy; exact drug catalog ingredient",
        "execution": "unavailable",
        "meanings": {
            "6dfc21ad0c907a82da22f10beaafcbdbc748aee492230536a2dd530c096bd53f": {
                "match": "exact_catalog_ingredient",
                "combination": "separate_product_not_components",
                "measure": "revenue",
            }
        },
        "scope": "nha thuoc",
    },
    "delivery_batches": {
        "selector": "giao nhieu dot",
        "requirements": "verified construction industry; delivered batch key/date; "
        "no sales-status substitution",
        "execution": "unavailable",
        "meanings": {
            "d65891faeeced25a6b3062161c353af1f76c3c3d313d108577992ae112eaa2e2": {
                "axis": "delivered_at",
                "grain": ["order", "batch"],
                "include_later_returned": True,
                "cutoff": "explicit_period",
            },
            "da23fee213378821dde8390e4b884ec7b5007b015c7d336ccb2ef722c00c93ee": {
                "axis": "delivered_at",
                "grain": ["order", "batch"],
                "include_later_returned": True,
                "cutoff": "as_of",
            },
        },
        "scope": "vat lieu xay dung",
    },
    "answerability": {
        "selector": "@answerability",
        "requirements": "distinguish missing source information from unavailable operator",
        "execution": "bounded",
        "meanings": {
            "519883ca7feb163b1873d94343676f0d7a0fc23bfe95cef9028a40c5c43faaa0": {
                "source": "released_only",
                "insufficient_data": "unanswerable",
            }
        },
        "scope": "general",
    },
    "no_inference": {
        "selector": "@no_inference",
        "requirements": "source fields; rule dependency evidence",
        "execution": "bounded",
        "meanings": {
            "034bdc4f9b9a506fe3c934cb2359a50b77b4cdde29ec5656328f78b452767212": {
                "missing_field": "no_substitution_unless_rule_authorizes"
            }
        },
        "scope": "general",
    },
    "reason_type": {
        "selector": "@reason_type",
        "requirements": "typed output validator",
        "execution": "bounded",
        "meanings": {
            "ce8966f521efb0bfc340f99aaf0d3b42adbbb5614ea5d8184b4f9af34460f432": {
                "reason": "optional_string",
                "invalid_reason": "unreadable_line",
            }
        },
        "scope": "general",
    },
    "source_alias": {
        "selector": "ten goi, nhan",
        "requirements": "split/business/kind namespace; unique source alias",
        "execution": "bounded",
        "meanings": {
            "3a5cf344734dbeac717c6b77f75d818678a071f5bdae4d0953b0034775778377": {
                "identity": "code_first",
                "aliases": "source_evidence",
            }
        },
        "scope": "general",
    },
    "status_domain": {
        "selector": "trang thai don",
        "requirements": "source status decoding and consistency",
        "execution": "bounded",
        "meanings": {
            "ebf58867b8e547d11b161f44e056d3c987de9bf369992bc53fbecdddc8397eb4": {
                "values": ["completed", "cancelled", "returned", "shipping"],
                "one_per_order": True,
            }
        },
        "scope": "general",
    },
    "ledger_grain": {
        "selector": "bang khong ghi theo don",
        "requirements": "README confirms no order grain",
        "execution": "bounded",
        "meanings": {
            "0d8b2b988785c929d4c6a18c8346c13b33bb27aa99ccaf310fca72d5f5be936e": {
                "grain": "recorded_event",
                "keep_identical_records": True,
                "infer_order_identity": False,
            }
        },
        "scope": "general",
    },
    "sheet_year": {
        "selector": "ngay khong ghi nam",
        "requirements": "verified sheet year; date role; native epoch",
        "execution": "bounded",
        "meanings": {
            "4b8d1ba2186603badf3b3820d9ab57cd57279e4261dd5826ff2f40d56cd72838": {
                "year": "containing_sheet"
            }
        },
        "scope": "general",
    },
    "submission": {
        "selector": "@submission",
        "requirements": "strict JSON serialization; evaluator is separate",
        "execution": "bounded",
        "meanings": {
            "4ff85cd490836d13446a14fb52e56d492d36809789e472cb3fc83651d9673d24": {
                "record": ["id", "type", "answer"],
                "score": {"correct": 1, "wrong": -1, "abstain": 0},
            }
        },
        "scope": "general",
    },
    "output_values": {
        "selector": "@output_values",
        "requirements": "typed output and result refs",
        "execution": "bounded",
        "meanings": {
            "7354b83730b28ab167d6ee016a806f65103519a9188f086d807cb4984e8b8c76": {
                "number": "no_unit_or_percent_symbol",
                "name": "source_value_without_label",
                "multi_number_order": "question_order",
            }
        },
        "scope": "general",
    },
    "output_number": {
        "selector": "number",
        "requirements": "strict typed output serializer/validator",
        "execution": "bounded",
        "meanings": {
            "cd11f9312967c88f1388bc725a388d30bda671ca745cf1ddcfa27900c2616718": {
                "format": "JSON_number"
            }
        },
        "scope": "general",
    },
    "output_percent": {
        "selector": "percent",
        "requirements": "strict typed output serializer/validator",
        "execution": "bounded",
        "meanings": {
            "93b26d41c0e5cfba93e4bcac54ee613829f5cbb9a98e80eb869995c3336c8d05": {
                "format": "percentage_number"
            }
        },
        "scope": "general",
    },
    "output_date": {
        "selector": "date",
        "requirements": "strict typed output serializer/validator",
        "execution": "bounded",
        "meanings": {
            "74c170d0e131d2df70f7f809da225ac5d3872a7519d844e65162636fc5d8845c": {
                "format": "ISO_day_or_month"
            }
        },
        "scope": "general",
    },
    "output_boolean": {
        "selector": "boolean",
        "requirements": "strict typed output serializer/validator",
        "execution": "bounded",
        "meanings": {
            "074b9b2af175d33dd6f9e2406f835143c678de4e3cf3b4b3cd719036211229bd": {
                "format": "JSON_boolean"
            }
        },
        "scope": "general",
    },
    "output_string": {
        "selector": "string",
        "requirements": "strict typed output serializer/validator",
        "execution": "bounded",
        "meanings": {
            "7a5b598042f0a22a506594026477c5655e67607d7a9358269af39e020362c7b6": {
                "format": "source_name"
            }
        },
        "scope": "general",
    },
    "output_list": {
        "selector": "list",
        "requirements": "strict typed output serializer/validator",
        "execution": "bounded",
        "meanings": {
            "41147082c9c3f13d9db812f673945ee416f5c9e654463399e3274566a5916a48": {
                "format": "ordered_JSON_array"
            }
        },
        "scope": "general",
    },
    "output_table": {
        "selector": "table",
        "requirements": "strict typed output serializer/validator",
        "execution": "bounded",
        "meanings": {
            "0d941f45081f9feb0616718ea221b244e77be7854d8548376129e6826f479be9": {
                "format": "columns_and_rows_numeric_cells"
            }
        },
        "scope": "general",
    },
    "output_unanswerable": {
        "selector": "unanswerable",
        "requirements": "strict typed output serializer/validator",
        "execution": "bounded",
        "meanings": {
            "16b62a139a5e8dfa5f6683e732fd42f982111852ca094f216f0be3199575b8fc": {
                "format": "optional_string_reason"
            }
        },
        "scope": "general",
    },
    "format_first": {
        "selector": "@format_first",
        "requirements": "strict answer validator",
        "execution": "bounded",
        "meanings": {
            "f09115a77b722cff1196c0b31651fc571aae2101337677a057661e69e8b4fce3": {
                "validation": "before_value_comparison"
            }
        },
        "scope": "general",
    },
    "numeric_comparison": {
        "selector": "@numeric_comparison",
        "requirements": "Decimal tokens and exact executor; scorer unit fixtures",
        "execution": "bounded",
        "meanings": {
            "71e5ac41edfa54b6dca747249d159acdeb88e065e6ac9d25fb18013d28bc973f": {
                "mode": "exact",
                "money_places": 0,
                "percent_places": 2,
                "float_noise_only": True,
            }
        },
        "scope": "general",
    },
    "string_comparison": {
        "selector": "@string_comparison",
        "requirements": "source alias evidence; exact accent-preserving entity matching",
        "execution": "bounded: source identity and scorer; not generic fuzzy query matching",
        "meanings": {
            "862b364336a8721d8041dfd1f55d93a187912be86f2d79bd20038295741db3e1": {
                "ignore": ["case", "whitespace", "punctuation"],
                "accept": ["all_source_names", "source_codes", "name_with_source_phone"],
                "other_accents": "exact",
            }
        },
        "scope": "general",
    },
    "list_order": {
        "selector": "@list_order",
        "requirements": "measure/expiry and source code; listing operators",
        "execution": "bounded: ranking only",
        "meanings": {
            "4dd57a786f4d631887452fec5b60ff87a0f924ae29d3eea2d3e6a3bdc53b6313": {
                "topk": "measure_desc_code_asc",
                "expiry": "earliest_product_expiry_then_code",
                "no_measure": "code_asc",
                "compare": "set_and_order",
            }
        },
        "scope": "general",
    },
    "table_semantics": {
        "selector": "@table_semantics",
        "requirements": "typed table; named measure refs",
        "execution": "bounded",
        "meanings": {
            "e5e8f6621670f4d86add0f3c1fa02a331eaf854f705c8900dfb5eb47d2763928": {
                "grain": "one_object_per_row",
                "first_column": "object_name",
                "measures": "question_order",
                "compare_columns": "name_else_position",
                "row_order": "ignored",
            }
        },
        "scope": "general",
    },
    "unit_conversion": {
        "selector": "don vi co ban, don vi lon",
        "requirements": "complete conversion authority; checked chain; no cross-shop factor",
        "execution": "bounded: preconverted quantity and declared base units; chain "
        "conversion unavailable",
        "meanings": {
            "d6d2a5b980cc2b0120df28b592e03124ba73221a06b3bbcdac8a6b3600de3348": {
                "basis": "per_business_per_product",
                "source": ["conversion_table", "README"],
                "absent_conversion": "base_unit_factor_1",
            }
        },
        "scope": "general",
    },
    "shift_cash": {
        "selector": "bao cao tien mat theo ca",
        "requirements": "cash report grain; payment clock; refunds; no revenue substitution",
        "execution": "unavailable",
        "meanings": {
            "3b60015c500d924dd1f38b5240cc9a53e879b2e33d5e2ae108558c6e79be53bc": {
                "axis": "paid_at",
                "method": "cash",
                "include_debt_payments": True,
                "sales_source": "orders_not_shift_report",
            }
        },
        "scope": "general",
    },
    "weekday_average": {
        "selector": "trung binh moi ngay thu x cua ky",
        "requirements": "same effective window and weekday on numerator/denominator",
        "execution": "bounded",
        "meanings": {
            "23b9d724fac40bbb1223753a60681835c87c906cc958a1f5b32c635931adaab9": {
                "denominator": "all_calendar_weekdays_in_period",
                "include_zero_event_days": True,
            }
        },
        "scope": "general",
    },
    "customer_loyalty": {
        "selector": "khach trung thanh nhat",
        "requirements": "eligible orders; customer identity; distinct day operator",
        "execution": "unavailable",
        "meanings": {
            "bfc2b0e31b621728d4a16cfab5eecc4594c2f4cd62be313377f43bb11a5e5838": {
                "measure": "distinct_purchase_days",
                "identity_ref": "customer_identity",
            }
        },
        "scope": "general",
    },
    "customer_aov_ranking": {
        "selector": "khach sop nhat",
        "requirements": "customer identity; eligible orders; per-customer qualification",
        "execution": "unavailable",
        "meanings": {
            "b05cfd8bd45d4a6b3e76c8ba9257ff4119cdc8598db826765fc681cb3388d273": {
                "measure": "revenue/order_count",
                "minimum_orders": 3,
                "inclusive": True,
            }
        },
        "scope": "general",
    },
    "customer_cancellations": {
        "selector": "khach hay doi y nhat",
        "requirements": "customer identity; cancelled orders in order-date period",
        "execution": "unavailable",
        "meanings": {
            "4df6e4a30eb371f543bcbd693d86b88dea024050d0430ba5e2f788cd7ce1eaa0": {
                "measure": "cancelled_order_count"
            }
        },
        "scope": "general",
    },
    "product_order_ranking": {
        "selector": "mat hang duoc ua chuong nhat",
        "requirements": "eligible orders; unique product/order association",
        "execution": "unavailable",
        "meanings": {
            "d30fa5cfdacff1eb92fe505d55281aa5e7f416c50df980817fbe6c33c511100d": {
                "measure": "distinct_orders_containing_product"
            }
        },
        "scope": "general",
    },
    "staff_workdays": {
        "selector": "nhan vien, tho cham chi nhat",
        "requirements": "staff identity; work/order/warranty attribution and date",
        "execution": "unavailable",
        "meanings": {
            "c54bfa4ba63e28d2d80055e0b065cb99118f428406e0087b4ae049be7a57de6a": {
                "measure": "distinct_workdays",
                "qualifies": "at_least_one_sold_or_worked_order",
                "include_warranty": True,
            }
        },
        "scope": "general",
    },
    "busiest_day": {
        "selector": "ngay dat khach nhat",
        "requirements": "eligible orders; daily group/ranking",
        "execution": "unavailable",
        "meanings": {
            "83fc1ea314fee7815f2545e4b8419dbf9d4ee9c007f9692acd6b073f14d65420": {
                "measure": "order_count",
                "group": "calendar_day",
            }
        },
        "scope": "general",
    },
    "peak_hour": {
        "selector": "gio cao diem",
        "requirements": "order clock; eligible distinct orders; hour grouping",
        "execution": "unavailable",
        "meanings": {
            "0a24a7c020a8076259b1dae6e7ba720e63609fa1bb4fb1fcd65411810e775686": {
                "axis": "order_clock",
                "interval": "[h,h+1)",
                "measure": "order_count",
                "output": "starting_hour_number",
            }
        },
        "scope": "general",
    },
    "supplier_count": {
        "selector": "nha cung cap chinh",
        "requirements": "supplier identity; purchase key/status/date",
        "execution": "unavailable",
        "meanings": {
            "a25c7c78c3e82ca56ac316055fd4af97f9b4575ce2d68abf9916128adf0bd3bf": {
                "measure": "distinct_purchase_headers",
                "exclude": "cancelled",
                "not": "purchase_amount",
            }
        },
        "scope": "general",
    },
    "combined_topping": {
        "selector": "topping ghi chung o voi mon",
        "requirements": "verified cafe; README grammar and included amount; no double count",
        "execution": "unavailable",
        "meanings": {
            "3515e6bd95d7146a6c3fefa45d49cb9ce2663f91d6c6e25e8572add7acab788b": {
                "grain": "separate_product",
                "allocate": ["quantity", "amount"],
                "same_as": "separate_topping_row",
                "subtract_from_dish": True,
            }
        },
        "scope": "ca phe",
    },
    "invoice_adjustments": {
        "selector": "tien giam voucher, tien giam nho dung diem, phi ship",
        "requirements": "verified mother/baby; nonmerchandise roles; invoice boundaries",
        "execution": "unavailable",
        "meanings": {
            "e579b40cb5a93b890513bd24343cb97d19997fb850407a6ee3bff36afa5195a1": {
                "sign": "positive_output",
                "axis": "invoice_date",
                "statuses": ["completed", "shipping"],
                "roles": ["voucher", "points", "shipping"],
            }
        },
        "scope": "me va be",
    },
    "baby_size": {
        "selector": "so sua, size bim",
        "requirements": "verified mother/baby; size/brand/category/unit catalog",
        "execution": "unavailable",
        "meanings": {
            "dc83353927a9528d863159d0faed2854fe377cbbe9627819b01780d3031f00d6": {
                "source": "catalog_size",
                "blank": "unclassified_even_if_name_has_number",
                "share_denominator": "all_diaper_package_quantities_same_period",
            }
        },
        "scope": "me va be",
    },
    "membership_new": {
        "selector": "khach thanh vien moi trong thang",
        "requirements": "verified mother/baby; complete member register",
        "execution": "unavailable",
        "meanings": {
            "fe26a30477489ced58163a99d02c154f8dc53d2b36e968644432e3ab29028e88": {
                "axis": "registration_date",
                "include_never_purchased": True,
            }
        },
        "scope": "me va be",
    },
    "repair_visits": {
        "selector": "luot xe, phieu",
        "requirements": "verified repair; visit grain; warranty classification; receipt total",
        "execution": "unavailable",
        "meanings": {
            "05c05731bb0c557cf271b251dc7194d73df6cbfe221fe14b4f273be953419633": {
                "grain": "one_row_one_visit",
                "include_warranty_in_count": True,
                "average_denominator": "charged_visits_only",
            }
        },
        "scope": "sua xe",
    },
    "repair_tasks": {
        "selector": "viec ghi trong o lam gi",
        "requirements": "verified repair; task grammar; warranty classification; no "
        "per-task money inference",
        "execution": "unavailable",
        "meanings": {
            "0522f41570dbc7f29d4898a9933cde8510c9f1c8fbe565cbff058f1709ed92ad": {
                "count": "one_per_visit_with_task",
                "bare_oil": "engine_oil_not_gear_oil",
                "bare_tube": "replace_not_patch",
                "electrical_excludes": ["bulb", "battery"],
                "exclude_warranty": True,
            }
        },
        "scope": "sua xe",
    },
    "vehicle_identity": {
        "selector": "dong xe, bien so",
        "requirements": "verified repair; model alias evidence; plate parser",
        "execution": "unavailable",
        "meanings": {
            "10f481e1cac70b99e5cb53d5a58d17c654ce2bfd449d27cad221c649ce3f4c34": {
                "model_alias": "source_case_or_abbreviation",
                "vehicle_count": "distinct_plates",
                "include_warranty": True,
            }
        },
        "scope": "sua xe",
    },
    "mechanic_visits": {
        "selector": "tho lam nhieu nhat",
        "requirements": "verified repair; mechanic identity and visits",
        "execution": "unavailable",
        "meanings": {
            "b262e6e08399761e39af6ba2fdb2789aeec6b1f8941539c9c8aeb237f303b28d": {
                "measure": "visit_count",
                "include_warranty": True,
            }
        },
        "scope": "sua xe",
    },
    "repair_warranty": {
        "selector": "bao hanh",
        "requirements": "verified repair; visit date; charged/warranty classification",
        "execution": "unavailable",
        "meanings": {
            "101d67721c7fd796bbc6f38785e7e9b67629dd144b6940ee439c94e3d1b0a4ab": {
                "numerator": "warranty_visits_in_period",
                "denominator": "charged_visits_in_period",
                "prior_original_visit_allowed": True,
            }
        },
        "scope": "sua xe",
    },
    "repeat_vehicle": {
        "selector": "khach quen, xe toi nhieu lan",
        "requirements": "verified repair; charged visits; plate identity",
        "execution": "unavailable",
        "meanings": {
            "9ae51e6638f761161c9a57347ab26e6ece5c4db2fd10ef4f490da8deb4515e83": {
                "identity": "license_plate",
                "minimum_charged_visits": 2,
                "period": "same",
                "output": "plate",
                "exclude_warranty": True,
            }
        },
        "scope": "sua xe",
    },
    "phone_tradein": {
        "selector": "thu may cu",
        "requirements": "verified phone industry; invoice and tradein keys; "
        "payment/debt separation",
        "execution": "unavailable",
        "meanings": {
            "a32f60b407015efcc18b2baf11f25a8d7c0097d6cecff226acf423bc658e8162": {
                "payable": "after_discount_and_tradein_including_debt",
                "deduct_tradein_from_revenue": False,
                "period_statuses": ["completed", "shipping"],
            }
        },
        "scope": "cua hang dien thoai",
    },
    "installment": {
        "selector": "tra gop",
        "requirements": "verified phone; all contracts and linked payment histories; "
        "exact due schedule",
        "execution": "unavailable",
        "meanings": {
            "255752752d93d587a84c97172df0ae73cfd7072b087a001c70fa2ffd580846ca": {
                "kind": "debt",
                "payments": "debt_collection_not_revenue",
                "upfront": "not_installment",
                "include_separate_debt_ledgers": True,
                "overdue": "due_date<cutoff AND paid<due",
            }
        },
        "scope": "cua hang dien thoai",
    },
    "phone_warranty": {
        "selector": "bao hanh",
        "requirements": "verified phone; IMEI; received_at; sold device quantities",
        "execution": "unavailable",
        "meanings": {
            "bb1f29aeee1c8841d2139fe83d2b381ca85ff6cb6c9098f5536d797766675c33": {
                "numerator": "warranty_receipt_visits_in_quarter",
                "denominator": "devices_sold_in_quarter_excluding_cancelled_returned",
                "prior_sale_allowed": True,
            }
        },
        "scope": "cua hang dien thoai",
    },
    "phone_model_brand": {
        "selector": "dong may, hang",
        "requirements": "verified phone; resolved full product name and sold-device identity",
        "execution": "unavailable",
        "meanings": {
            "7f6d00cf101cbdb888bc3263926591f0581710c42b59214ca28ca97e8a176d4e": {
                "model": "name_before_capacity_GB_else_whole_name",
                "4G_is_capacity": False,
                "brand": "first_word",
                "iphone_brand": "Apple",
                "tradein_is_sale": False,
            }
        },
        "scope": "cua hang dien thoai",
    },
    "note_status": {
        "selector": "so khong co cot trang thai",
        "requirements": "README note role; order inheritance; explicit note/status parser",
        "execution": "unavailable",
        "meanings": {
            "4406664684f28cff70ab29903f8d261a88f11db37592828e63ccdc35ae1c6b0b": {
                "source": "order_note",
                "no_status_mention": "completed",
                "condition": "no_status_column",
            }
        },
        "scope": "general",
    },
}
