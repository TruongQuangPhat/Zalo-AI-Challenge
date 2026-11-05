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
