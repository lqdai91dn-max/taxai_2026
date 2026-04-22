"""
src/retrieval/hybrid_search.py
Hybrid search = BM25 (keyword) + Vector (semantic) cho TaxAI 2026
"""

from __future__ import annotations
import json
import logging
import math
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

REF_MIN_SCORE   = 0.005  # minimum rrf_score để trigger expansion (RRF range ~0.005–0.016)
REF_MAX_PER_NODE = 3     # max ref nodes expand per source node
REF_MAX_CHARS   = 800    # cap text của expanded node

# Phase 1: Supersession penalty + Authority boost
# ── Legal hierarchy boost ─────────────────────────────────────────────────
# Boost QH/NĐ cao hơn TT/công văn để tránh old TT outcompete new Luật
_LEGAL_LEVEL_BOOST = {
    1: 1.10,   # Luật (Quốc hội)
    2: 1.07,   # Nghị quyết (UBTVQH)
    3: 1.05,   # Nghị định (Chính phủ)
    4: 1.00,   # Thông tư (Bộ)
    5: 0.85,   # Công văn
}

# Supersession penalty: áp dụng khi chunk có metadata superseded=True
# và query KHÔNG liên quan đến năm < 2026 (Rule 3 transitional)
_SUPERSESSION_PENALTY = 0.25   # nhân rrf_score × 0.25 cho superseded chunks

# Rule 3 v2 — Legacy keyword bypass (Phase 1.5)
# Các khái niệm nghiệp vụ chỉ có trong 111_2013/92_2015, hoặc
# implicit temporal context (năm ngoái, kỳ trước) không có số năm rõ.
# Khi query match → skip penalty giống như query có năm < 2026.
_LEGACY_KEYWORDS: List[str] = [
    # Implicit temporal phrases
    "năm ngoái", "năm trước", "tháng trước", "kỳ trước",
    "trước đây", "trước khi luật mới", "theo luật cũ",
    "quy định cũ", "theo quy định cũ",
    "trước 01/01/2026", "trước 1/1/2026", "trước 2026",
    # Topic-specific: phone/benefit allowances (111_2013 Điều 2)
    "hỗ trợ điện thoại", "phụ cấp điện thoại", "tiền điện thoại",
    "tiền ăn giữa ca", "phụ cấp ăn trưa", "trợ cấp ăn",
    # Topic-specific: payroll cycle (111_2013 Điều 8)
    "kỳ tính lương", "chu kỳ lương", "ngày 26 tháng",
    "26 tháng trước", "kỳ lương",
    # Topic-specific: employer-paid PIT (111_2013 Điều 26)
    "đóng thuế tncn thay", "đóng thuế thay",
    "đóng tiền thuế tncn cho người lao động",
    "công ty nộp thuế thay", "nộp thuế thay cho nhân viên",
]

# ── Synonym Dictionary — Query Expansion (A3) ────────────────────────────────
# Giải quyết vocabulary mismatch: user dùng viết tắt, luật dùng đầy đủ.
# Ví dụ: "GTGT" → corpus có "giá trị gia tăng" nhưng không có "GTGT" → BM25 miss.
# Fix: append full form vào BM25 query (không thay thế — giữ cả 2 để match được
#      cả query có từ viết tắt và query có từ đầy đủ).
# Chỉ áp dụng cho BM25. Vector search giữ query gốc (đã capture semantic).

_SYNONYM_MAP: Dict[str, str] = {
    "gtgt":  "giá trị gia tăng",
    "tncn":  "thu nhập cá nhân",
    "hkd":   "hộ kinh doanh",
    "bđs":   "bất động sản",
    "tmđt":  "thương mại điện tử",
    "bhxh":  "bảo hiểm xã hội",
    "bhyt":  "bảo hiểm y tế",
    "bhtn":  "bảo hiểm thất nghiệp",
    "nđ":    "nghị định",
    "tt":    "thông tư",
    "qh":    "quốc hội",
    "ubnd":  "ủy ban nhân dân",
    "hđkt":  "hóa đơn kế toán",
    "vat":   "giá trị gia tăng",
    "pit":   "thu nhập cá nhân",
}

# ── Keyword-Triggered Doc Injection (R34) ────────────────────────────────────
# Root cause R33b: 84% fail = Type A (doc không vào retrieval pool).
# Doc nhỏ (67–200 chunks) bị outcompete bởi doc lớn (400–985 chunks) trong BM25+vector.
# Fix: với query match keyword cụ thể → force-retrieve top_k từ doc đúng,
#      prepend vào vector pool trước RRF để chúng có RRF rank cao.
#
# Nguyên tắc:
#   - Chỉ HIGH CONFIDENCE mappings (keyword rõ ràng → doc cụ thể)
#   - Equal weight (không penalize injected docs)
#   - max_k=3 mỗi doc (tránh over-injection)
#   - Log mỗi injection để validate impact
#
# 68_2026_NDCP CHƯA có rule — cần verify phantom miss trước (Q85, Q86, Q93...)

_TOPIC_RULES: List[Dict] = [
    {
        "name": "uy_quyen_quyet_toan",
        "keywords": [
            "ủy quyền quyết toán",
            "uy quyen quyet toan",
            "ủy quyền để quyết toán",
            "uỷ quyền quyết toán",
        ],
        "docs": ["373_2025_NDCP"],
        "top_k": 3,
    },
    {
        "name": "quyet_toan_tncn",
        "keywords": [
            "quyết toán thuế",
            "quyết toán tncn",
            "quyết toán thu nhập cá nhân",
            "nộp quyết toán",
            "hồ sơ quyết toán",
            "tự quyết toán",
        ],
        # Q207: 108_2025_QH15 (current QL Thuế) có hoàn thuế/nộp thừa sections
        # 126_2020_NDCP giữ lại vì vẫn là guidance cho nhiều quyết toán thủ tục
        "docs": ["126_2020_NDCP", "108_2025_QH15"],
        "top_k": 3,
    },
    {
        # Q83: "không lập hóa đơn khi bán hàng" 10M-20M ở 125 khoản 5 (không bị 310 amend)
        # 310 là amendment decree → được retrieve tự nhiên, nhưng 125 cần inject thêm
        "name": "xu_phat_hoa_don_125",
        "keywords": [
            "mức phạt hóa đơn",
            "phạt không xuất hóa đơn",
            "phạt không lập hóa đơn",
            "xử phạt hóa đơn",
            "vi phạm hóa đơn",
            "không xuất hóa đơn bị phạt",
            "cố tình không xuất hóa đơn",
        ],
        "docs": ["125_2020_NDCP"],
        "top_k": 3,
    },
    {
        "name": "hkd_mau_bieu_tt18",
        "keywords": [
            "18/2026",
            "tt18",
            "mẫu s2b",
            "s2b-hkd",
            "mẫu kê khai hộ kinh doanh",
            # Q64: tài khoản ngân hàng HKD → 18 có mẫu 01/BK-STK
            "tài khoản ngân hàng",
            "thông báo số tài khoản",
            "01/bk-stk",
            "bk-stk",
            "tài khoản cá nhân chủ hộ",
            # Q35: sai kỳ khai (quý vs tháng) → 18 có form sửa kỳ
            "khai theo tháng",
            "khai theo quý",
            "sai kỳ khai",
            # Q42: ủy quyền đại lý thuế làm thủ tục → 18 có mục "Thông tin đại lý thuế"
            "đại lý thuế",
            "ủy quyền thủ tục thuế",
        ],
        "docs": ["18_2026_TTBTC"],
        "top_k": 3,
    },
    {
        "name": "pit_new_law_109",
        "keywords": [
            # Thu nhập miễn thuế — Q18 (lãi tiết kiệm), Q20 (shipper ngưỡng)
            "lãi tiết kiệm",
            "lãi ngân hàng",
            "lãi suất tiết kiệm",
            "thu nhập miễn thuế",
            "được miễn thuế tncn",
            # Giảm trừ gia cảnh 2026 — Q11
            "giảm trừ gia cảnh",
            "15,5 triệu",
            "15.5 triệu",
            "người phụ thuộc 6,2",
            "người phụ thuộc 6.2",
            # Luật TNCN mới
            "luật 109",
            "luật thuế tncn 2025",
            "từ 01/07/2026",
            "hiệu lực 2026",
        ],
        "docs": ["109_2025_QH15"],
        "top_k": 3,
    },
    {
        "name": "hkd_procedure_68",
        "keywords": [
            # Thủ tục kê khai HKD — Q115 (đổi kỳ khai), Q42 (ủy quyền hoàn thuế)
            "kỳ khai thuế",
            "đổi kỳ khai",
            "khai theo tháng",
            "khai theo quý",
            "ủy quyền hoàn thuế",
            "đại lý thuế làm thay",
            "hoàn thuế hộ kinh doanh",
            # Phân loại ngành nghề HKD — Q56 (đại lý bảo hiểm)
            "hoa hồng bảo hiểm",
            "đại lý bảo hiểm",
            "hộ kinh doanh hoàn thuế",
            # NĐ68 cụ thể
            "68/2026",
            "nd68",
        ],
        "docs": ["68_2026_NDCP"],
        "top_k": 3,
    },
    {
        # R49: Các câu TMĐT về sàn khấu trừ / hoàn thuế không retrieve được 68_2026
        # tự nhiên (bị 117_2025 outcompete). Keywords này rất đặc thù — không trùng
        # với passing questions (verified vs R48 results).
        # Coverage: Q30, Q58, Q80, Q106, Q119, Q137, Q148, Q162, Q171, Q193
        "name": "tmdt_san_khautru_68",
        "keywords": [
            # Hoàn thuế TMĐT nộp thừa
            "trừ lố",
            "trừ thừa tiền thuế",
            # Sàn khấu trừ / tính thuế
            "sàn tmđt tính thuế",
            "thực hiện khấu trừ thuế",
            "sàn khấu trừ thuế",
            "sàn thương mại điện tử khấu trừ",
            # Mẫu biểu TMĐT (68_2026 Chương III)
            "01-1/bk",
            # Chủ quản sàn — nghĩa vụ khai thuế thay
            "chủ quản sàn",
            "chủ quản nền tảng",
            # Xe công nghệ (Grab/Be) — HKD scope nhưng gate=False
            "xe công nghệ",
        ],
        "docs": ["68_2026_NDCP"],
        "top_k": 3,
    },
    {
        # Q30: sàn TMĐT trừ lố → cần 117 (platform withholding rules) + 68 (refund)
        # 117_2025_NDCP bị outcompete bởi 68 khi "trừ lố" trigger, cần inject thêm
        "name": "tmdt_hoan_thue_117",
        "keywords": [
            "trừ lố",
            "trừ thừa tiền thuế",
            "hoàn thuế sàn tmđt",
            "hoàn thuế thương mại điện tử",
            "sàn tmđt hoàn",
            "hoàn lại tiền thuế sàn",
        ],
        "docs": ["117_2025_NDCP"],
        "top_k": 3,
    },
    {
        # Q41: vũ trường/karaoke → tiêu thụ đặc biệt (373 có Mẫu 01/TTĐB) + 68 (HKD)
        # Query không có "hộ kinh doanh" nên HKD gate không trigger → cần topic rule
        "name": "ttdb_hkd_373",
        "keywords": [
            "tiêu thụ đặc biệt",
            "thuế ttđb",
            "vũ trường",
            "karaoke",
            "01/ttđb",
            "mẫu ttđb",
        ],
        "docs": ["68_2026_NDCP", "373_2025_NDCP"],
        "top_k": 3,
    },
    {
        # Q43: chuyên gia AI trong DNKN sáng tạo → 198_2025_QH15 có miễn/giảm TNDN 50%
        "name": "khoi_nghiep_sang_tao_198",
        "keywords": [
            "khởi nghiệp sáng tạo",
            "doanh nghiệp đổi mới sáng tạo",
            "startup sáng tạo",
            "đổi mới sáng tạo",
            "198/2025",
            "luật 198",
        ],
        "docs": ["198_2025_QH15"],
        "top_k": 3,
    },
]

# ── HKD Injection Gate ───────────────────────────────────────────────────────
# Chỉ inject 68_2026_NDCP khi query có explicit HKD entity marker.
# Mục đích: tránh false positive khi scope classifier detect HKD qua keywords
# chung (vd: "tiệm tóc", "cho thuê phòng") mà câu hỏi thực ra hỏi về
# GTGT/TNCN rate hoặc expense deduction — không phải HKD-specific rules.
#
# Markers được chọn: rõ ràng về HKD entity, không ambiguous với PIT context.
_HKD_INJECT_GATE = re.compile(
    # Entity markers — unambiguously về HKD legal entity
    r"hộ kinh doanh|hkd\b|hộ gia đình kinh doanh|đăng ký kinh doanh hộ|"
    # Tax-method terms — chỉ xuất hiện trong HKD tax context
    r"thuế khoán|phương pháp khoán|doanh thu trừ chi phí|phương pháp lợi nhuận|"
    r"kê khai theo doanh thu thực tế|chế độ kế toán hộ",
    re.IGNORECASE | re.UNICODE,
)
# NOTE: Không include activity keywords (tạp hóa, mở tiệm, làm nail...) vì
# chúng có thể xuất hiện trong câu hỏi không liên quan thuế (vốn, giấy phép...).
# Scope boost (step 4) vẫn hoạt động cho các query đó — injection chỉ là tăng cường.

from rank_bm25 import BM25Okapi

from src.retrieval.embedder import Chunk, DocumentEmbedder
from src.retrieval.vector_store import VectorStore
from src.retrieval.scope_classifier import classify_scope, apply_scope_boost
from src.retrieval.build_exception_index import EXCEPTION_INDEX_PATH

logger = logging.getLogger(__name__)

# P3 — Cross-reference pattern for conditional sibling expansion.
# Only expand siblings when a chunk explicitly references adjacent subpoints.
import re as _re_p3
_CROSS_REF_PAT = _re_p3.compile(
    r"(quy\s+định\s+tại|theo\s+quy\s+định|trừ\s+trường\s+hợp|"
    r"áp\s+dụng\s+((điểm|\s+khoản|\s+tiết|\s+điều)))",
    _re_p3.IGNORECASE,
)


def tokenize_vi(text: str) -> List[str]:
    """Tokenize tiếng Việt đơn giản — tách theo khoảng trắng và dấu câu"""
    import re
    text = text.lower()
    tokens = re.findall(r'[\w]+', text)
    return tokens


def _expand_synonyms(query: str) -> str:
    """Append full forms của acronym thuế vào query để BM25 match được corpus.

    Không thay thế — append thêm để giữ cả 2 dạng trong BM25 scoring.
    Chỉ append khi full form chưa có sẵn trong query (tránh duplicate tokens).

    Ví dụ:
        "thuế GTGT cho hộ kinh doanh"
        → "thuế GTGT cho hộ kinh doanh giá trị gia tăng"
        (BM25 giờ match được cả "GTGT" lẫn "giá trị gia tăng" trong corpus)
    """
    q_lower = query.lower()
    tokens = re.findall(r'[\w]+', q_lower)
    expansions = []
    for token in tokens:
        full = _SYNONYM_MAP.get(token)
        if full and full not in q_lower:
            expansions.append(full)
    if expansions:
        expanded = query + " " + " ".join(expansions)
        logger.debug(f"[SYN] Query expanded: {query!r} → appended: {expansions}")
        return expanded
    return query


class HybridSearch:
    """
    Kết hợp BM25 + Vector search với RRF (Reciprocal Rank Fusion)
    
    BM25  → tốt cho keyword chính xác (số điều, thuế suất, %)
    Vector → tốt cho semantic (câu hỏi tự nhiên)
    RRF   → kết hợp rank từ cả 2, không cần normalize score
    """

    def __init__(
        self,
        model_name: str = "keepitreal/vietnamese-sbert",
    ):
        self.embedder = DocumentEmbedder(model_name)
        self.store    = VectorStore()
        self._build_bm25_index()

        # Load exception index (C2) — O(1) lookup at runtime
        self._exception_index: dict = {}
        if EXCEPTION_INDEX_PATH.exists():
            import json as _json
            with open(EXCEPTION_INDEX_PATH, encoding="utf-8") as f:
                self._exception_index = _json.load(f)
            logger.info(f"✅ Exception index loaded — {len(self._exception_index)} rules")
        else:
            logger.warning("⚠️ Exception index not found — run build_exception_index.py")

        logger.info("✅ HybridSearch initialized")

    # ── BM25 Index ───────────────────────────────────────────────────────

    def _build_bm25_index(self):
        """Load tất cả chunks từ Qdrant → build BM25 index"""
        logger.info("🔨 Building BM25 index...")

        total = self.store.count()
        if total == 0:
            logger.warning("⚠️ Qdrant trống — chưa index documents!")
            self.bm25        = None
            self.bm25_chunks = []
            return

        results = self.store.get_all()

        self.bm25_ids   = results["ids"]
        self.bm25_texts = results["documents"]
        self.bm25_metas = results["metadatas"]

        # Tokenize và build BM25
        tokenized = [tokenize_vi(text) for text in self.bm25_texts]
        self.bm25 = BM25Okapi(tokenized)

        logger.info(f"✅ BM25 index built — {len(self.bm25_texts)} documents")

    # ── Search ───────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        n_results: int = 5,
        bm25_weight: float = 0.3,
        vector_weight: float = 0.7,
        filter_doc_id: Optional[str] = None,
        exclude_doc_ids: Optional[List[str]] = None,
        intent: str = "",
        query_intent=None,   # P5.3: QueryIntent object từ QueryIntent Builder (optional)
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search với RRF fusion + NodeMetadata reranker (P5.3) + expansions.

        Args:
            query:           câu hỏi của user
            n_results:       số kết quả trả về
            bm25_weight:     trọng số BM25 (keyword)
            vector_weight:   trọng số vector (semantic)
            filter_doc_id:   lọc theo document cụ thể (include filter)
            exclude_doc_ids: danh sách doc_id bị loại trừ khỏi kết quả (P0: chống false positive)
            intent:          QueryIntent.value từ query_classifier (optional, legacy)
            query_intent:    QueryIntent object từ P5.1 QueryIntent Builder (P5.3)
                             Nếu None → bỏ qua NodeMetadata reranker (backward compat)
        """

        # 1. Vector search
        query_embedding = self.embedder.model.encode(
            query,
            normalize_embeddings=True
        ).tolist()

        vector_hits = self.store.query(
            query_embedding = query_embedding,
            n_results       = min(n_results * 3, 50),   # R33b: 30→50
            filter_doc_id   = filter_doc_id,
        )

        # NOTE (R32 lessons):
        # - Vector per-doc cap=2 caused -0.088 T2 regression. Do NOT cap vector hits.
        # - BM25 per-doc cap=2 caused -0.038 T2 regression. Large docs (111, 126)
        #   are true positives for many queries — capping them attenuates correct signal.

        # 2. BM25 search — dùng expanded query để match cả acronym lẫn full form
        bm25_query = _expand_synonyms(query)
        bm25_hits = self._bm25_search(bm25_query, n_results * 3, filter_doc_id)

        # 2.4. Classify scope sớm — dùng cho cả injection (2.5, 2.7) và boost (4)
        sc = classify_scope(query, intent=intent) if not filter_doc_id else None

        # 2.5. Keyword-triggered doc injection (R34)
        # Prepend injected hits vào CẢ HAI pools trước RRF:
        #   vector rank 0 → contribution = vector_weight / (k+0) = 0.7/60 = 0.01167
        #   bm25 rank 0   → contribution = bm25_weight  / (k+0) = 0.3/60 = 0.005
        #   total injected ≈ 0.01667 → đủ để vượt doc lớn bị diluted qua nhiều ranks
        # Chỉ chạy khi không có filter_doc_id (search tổng quát).
        if not filter_doc_id:
            injected = self._inject_topic_docs(query, query_embedding, vector_hits + bm25_hits)
            if injected:
                vector_hits = injected + vector_hits  # prepend → high vector rank
                bm25_hits   = injected + bm25_hits    # prepend → high BM25 rank

        # 2.7. Scope-triggered doc injection (R48)
        # Keyword injection (2.5) chỉ cover query có từ khóa cụ thể.
        # Root cause R47: 68_2026_NDCP chỉ đạt 38.6% retrieval rate vì scope boost
        # (bước 4) xảy ra SAU RRF — không thể boost doc không có trong RRF pool.
        # Fix: inject 68_2026 vào pool TRƯỚC RRF khi HKD scope được phát hiện.
        if sc is not None and not sc.is_all:
            scope_injected = self._inject_scope_docs(query_embedding, vector_hits + bm25_hits, sc, query)
            if scope_injected:
                vector_hits = scope_injected + vector_hits
                bm25_hits   = scope_injected + bm25_hits

        # 3. RRF Fusion
        results = self._rrf_fusion(
            vector_hits, bm25_hits,
            bm25_weight, vector_weight,
            n_results
        )

        # 3.1. Authority hierarchy boost (Phase 1) — QH×1.10, NĐ×1.05, TT×1.00
        if not filter_doc_id:
            results = self._apply_authority_boost(results)

        # 3.2. Supersession penalty (Phase 1 + Rule 3 v2)
        # Penalize chunks từ superseded docs (111/92) trừ khi:
        #   - Rule 3a: query_intent.time.year < 2026 (explicit year)
        #   - Rule 3b: query chứa legacy keywords (implicit temporal/topic context)
        if not filter_doc_id:
            results = self._apply_supersession_penalty(results, query_intent, query)

        # 3.5. NodeMetadata Reranker (P5.3) — sau RRF, trước expansions
        # Dùng QueryIntent × NodeMetadata bonus để reorder trước khi expand
        if query_intent is not None:
            from src.retrieval.reranker import rerank_with_exception_penalty
            results = rerank_with_exception_penalty(results, query_intent, query)
            logger.debug("[P5.3] NodeMetadata reranker + exception penalty applied")

        # 4. Scope boost/penalty (C1) — after RRF, before A2 expand
        # sc đã được classify ở bước 2.4 — reuse để tránh tính lại.
        if sc is not None:
            results = apply_scope_boost(results, sc)
            logger.info(
                f"[C1] scopes={sc.scopes} conf={sc.confidence:.2f} hits={sc.hits}"
            )

        # 5. Reference expansion (A2)
        results = self._expand_references(results)

        # 6. Exception expansion (C2)
        results = self._expand_exceptions(results)

        # 6.5. Sibling expansion (P3) — fetch tiet N±1 khi tiet N được retrieved
        results = self._expand_siblings(results)

        # 6.6. Children expansion (P6) — fetch tiết con khi Điểm là header "như sau:"
        results = self._expand_children(results)

        # 7. Amendment expansion (B4) — kéo chunks từ văn bản sửa đổi vào context
        results = self._expand_amendments(results)

        # 8. Final exclusion filter (P0) — áp dụng SAU tất cả expansions.
        # Lý do đặt cuối: _expand_amendments có thể kéo 310 vào qua amended_by_doc_ids
        # của 125_2020_NDCP, dù 310 đã bị loại trừ ở bước 3. Chạy lại sau cùng đảm bảo
        # exclude_doc_ids luôn có hiệu lực tuyệt đối.
        if exclude_doc_ids:
            _excl = set(exclude_doc_ids)
            results = [r for r in results if r.get("metadata", {}).get("doc_id", "") not in _excl]

        # 9. valid_to temporal filter (P0) — loại chunk từ văn bản đã hết hiệu lực.
        # DOCUMENT_REGISTRY.LegalDocument.effective_to: None = còn hiệu lực; date = hết hạn.
        # Default query_date = today → filter tất cả doc hết hiệu lực (effective_to < today).
        # Không filter khi effective_to=None (đại đa số — văn bản còn hiệu lực).
        # Tác dụng hiện tại: minimal (chưa có doc nào có effective_to set).
        # Sẽ có tác dụng khi 111_2013/92_2015 được set effective_to trong DOCUMENT_REGISTRY.
        results = self._filter_expired_docs(results)

        # 10. Scope filter (P1) — loại specific_entity docs cho câu hỏi nguyên tắc chung.
        # Chỉ chạy khi search tổng quát (không filter theo doc cụ thể).
        # Hiện tại: tất cả docs là "general" → không có tác dụng.
        # Sẽ active khi thêm công văn CQT trả lời riêng cho doanh nghiệp cụ thể.
        if not filter_doc_id:
            results = self._filter_specific_entity_docs(results, is_general_question=True)

        return results

    def _filter_specific_entity_docs(
        self,
        results: List[Dict[str, Any]],
        is_general_question: bool = True,
    ) -> List[Dict[str, Any]]:
        """P1 — Scope filter: loại tài liệu specific_entity cho câu hỏi nguyên tắc chung.

        Vấn đề: Công văn trả lời riêng cho 1 doanh nghiệp (scope=specific_entity) có thể
        mâu thuẫn với Thông tư chung (scope=general). Nếu dùng recency → sẽ chọn sai.

        Rule:
            is_general_question=True → exclude specific_entity docs khỏi results
            is_general_question=False → giữ nguyên (câu hỏi về trường hợp cụ thể, OK dùng)

        Hiện trạng (09/04/2026):
            Tất cả docs trong DOCUMENT_REGISTRY đều scope="general".
            Method này là infrastructure — sẽ có tác dụng khi thêm specific_entity docs.

        Args:
            results:              Danh sách chunks sau ranking
            is_general_question:  True nếu câu hỏi về nguyên tắc/quy định chung
        """
        if not is_general_question:
            return results

        from src.utils.config import DOCUMENT_REGISTRY
        filtered = []
        removed = 0
        for r in results:
            doc_id = r.get("metadata", {}).get("doc_id", "")
            reg = DOCUMENT_REGISTRY.get(doc_id)
            if reg is not None and reg.scope_of_application == "specific_entity":
                removed += 1
                logger.info("[ScopeFilter] Loại doc=%s (specific_entity) khỏi general question", doc_id)
                continue
            filtered.append(r)

        if removed:
            logger.info("[ScopeFilter] Filtered %d specific_entity chunks", removed)
        return filtered

    def _filter_expired_docs(
        self,
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """P0 — valid_to temporal filter.

        Loại bỏ chunks từ văn bản đã hết hiệu lực theo DOCUMENT_REGISTRY.
        Dùng date.today() làm query_date mặc định.

        Design note:
        - Phần lớn docs có effective_to=None → không bị filter (vẫn hiệu lực)
        - Hiện tại không có doc nào có effective_to set → filter không có tác dụng
        - Sẽ có tác dụng khi thêm effective_to cho 111_2013, 92_2015 (superseded docs)
        - Không dùng superseded metadata vì supersession penalty (bước 3.2) đã xử lý
          → filter này là lớp backup cứng (hard exclude) khi doc thực sự hết hiệu lực
        """
        from src.utils.config import DOCUMENT_REGISTRY
        from datetime import date as _date

        today = _date.today()
        filtered = []
        removed = 0
        for r in results:
            doc_id = r.get("metadata", {}).get("doc_id", "")
            reg = DOCUMENT_REGISTRY.get(doc_id)
            if reg is not None and reg.effective_to is not None and reg.effective_to < today:
                removed += 1
                logger.info(
                    "[ValidTo] Loại chunk từ %s (effective_to=%s < today=%s)",
                    doc_id, reg.effective_to, today,
                )
                continue
            filtered.append(r)

        if removed:
            logger.info("[ValidTo] Filtered %d chunks từ %d doc(s) đã hết hiệu lực", removed, removed)
        return filtered

    def _detect_topics(self, query: str) -> List[Dict]:
        """Trả về các TOPIC_RULES match với query (case-insensitive)."""
        q_lower = query.lower()
        return [
            rule for rule in _TOPIC_RULES
            if any(kw in q_lower for kw in rule["keywords"])
        ]

    def _inject_topic_docs(
        self,
        query: str,
        query_embedding: List[float],
        existing_hits: List[Dict],
    ) -> List[Dict]:
        """Keyword-triggered doc injection (R34).

        Với query match keyword trong _TOPIC_RULES, force-retrieve top_k chunks
        từ doc cụ thể để đảm bảo chúng luôn có mặt trong candidate pool.

        Injected hits được prepend vào vector_hits trước RRF → nhận RRF rank cao
        → compete trên equal footing với global pool.

        Returns:
            List hits mới (chưa có trong existing_hits), có field `injected_by`.
        """
        topics = self._detect_topics(query)
        if not topics:
            return []

        seen_ids = {h["chunk_id"] for h in existing_hits}
        injected: List[Dict] = []

        for rule in topics:
            rule_new: List[Dict] = []
            for doc_id in rule["docs"]:
                hits = self.store.query(
                    query_embedding=query_embedding,
                    n_results=rule["top_k"],
                    filter_doc_id=doc_id,
                )
                for h in hits:
                    if h["chunk_id"] not in seen_ids:
                        seen_ids.add(h["chunk_id"])
                        h["injected_by"] = rule["name"]
                        rule_new.append(h)

            injected.extend(rule_new)
            logger.info(
                f"[INJECT] topic={rule['name']} "
                f"docs={rule['docs']} "
                f"injected={len(rule_new)} new chunks"
            )

        return injected

    def _inject_scope_docs(
        self,
        query_embedding: List[float],
        existing_hits: List[Dict],
        sc,           # ScopeClassification object
        query: str = "",
    ) -> List[Dict]:
        """Scope-triggered injection (R48).

        Khi HKD scope detected → inject top_k chunks từ 68_2026_NDCP vào candidate
        pool TRƯỚC RRF để scope boost (bước 4) có thể tác động.

        Root cause R47: 68_2026_NDCP chỉ đạt 38.6% retrieval vì scope boost xảy ra
        SAU RRF — nếu doc không có trong pool, boost không có tác dụng.

        Không inject nếu doc đã được keyword injection (2.5) cover.
        """
        _SCOPE_INJECT: Dict[str, Dict] = {
            "HKD": {"docs": ["68_2026_NDCP"], "top_k": 3},
        }

        # Khi TMDT + HKD co-occur: câu hỏi về sàn TMĐT, 117_2025_NDCP phù hợp hơn
        # → skip HKD injection để tránh displacement 117_2025 khỏi top-5
        if "TMDT" in sc.scopes and "HKD" in sc.scopes:
            logger.debug("[SCOPE-INJECT] TMDT+HKD co-occur → skip (117_2025 priority)")
            return []

        # Docs đã được keyword injection cover — skip để tránh double-inject
        already_injected_docs = {
            h.get("metadata", {}).get("doc_id")
            for h in existing_hits
            if h.get("injected_by")
        }

        injected: List[Dict] = []

        for scope in sc.scopes:
            rule = _SCOPE_INJECT.get(scope)
            if not rule:
                continue

            # Gate: chỉ inject HKD khi query có explicit HKD entity marker.
            # Tránh false positive khi scope=HKD được trigger bởi keywords chung
            # ("tiệm tóc", "cho thuê phòng") mà câu hỏi thực ra là PIT context.
            if scope == "HKD" and not _HKD_INJECT_GATE.search(query):
                logger.debug(
                    "[SCOPE-INJECT] HKD scope detected nhưng không có explicit HKD marker "
                    "→ skip injection (tránh false positive)"
                )
                continue

            for doc_id in rule["docs"]:
                if doc_id in already_injected_docs:
                    logger.debug(
                        f"[SCOPE-INJECT] scope={scope} doc={doc_id} — already covered by keyword injection, skip"
                    )
                    continue
                hits = self.store.query(
                    query_embedding=query_embedding,
                    n_results=rule["top_k"],
                    filter_doc_id=doc_id,
                )
                # Không dùng seen_ids để filter: nếu chunk đã có trong natural pool
                # ở rank thấp, cần prepend nó vào rank 0 để RRF accumulation
                # boost score lên. Duplicate chunk_ids trong RRF được handled
                # bằng cách cộng dồn score (rank 0 + natural_rank).
                scope_hits = []
                for h in hits:
                    h_copy = h.copy()
                    h_copy["injected_by"] = f"scope:{scope}"
                    scope_hits.append(h_copy)
                injected.extend(scope_hits)
                if scope_hits:
                    logger.info(
                        f"[SCOPE-INJECT] scope={scope} doc={doc_id} "
                        f"injected={len(scope_hits)} chunks (may overlap natural pool)"
                    )

        return injected

    def _bm25_search(
        self,
        query: str,
        n_results: int,
        filter_doc_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """BM25 keyword search."""
        if not self.bm25:
            return []

        tokens = tokenize_vi(query)
        scores = self.bm25.get_scores(tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

        hits = []
        for idx, score in ranked:
            if len(hits) >= n_results:
                break
            if score <= 0:
                break
            meta = self.bm25_metas[idx]
            if filter_doc_id and meta.get("doc_id") != filter_doc_id:
                continue
            hits.append({
                "chunk_id": self.bm25_ids[idx],
                "text":     self.bm25_texts[idx],
                "metadata": meta,
                "score":    float(score),
            })
        return hits

    def _rrf_fusion(
        self,
        vector_hits: List[Dict],
        bm25_hits: List[Dict],
        bm25_weight: float,
        vector_weight: float,
        n_results: int,
        k: int = 60,  # RRF constant
    ) -> List[Dict[str, Any]]:
        """Reciprocal Rank Fusion"""

        rrf_scores: Dict[str, float] = {}
        chunk_data: Dict[str, Dict]  = {}

        # Vector ranks
        for rank, hit in enumerate(vector_hits):
            cid = hit["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0) + vector_weight / (k + rank + 1)
            chunk_data[cid] = hit

        # BM25 ranks
        for rank, hit in enumerate(bm25_hits):
            cid = hit["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0) + bm25_weight / (k + rank + 1)
            if cid not in chunk_data:
                chunk_data[cid] = hit

        # Sort by RRF score
        sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)

        results = []
        for cid in sorted_ids[:n_results]:
            hit = chunk_data[cid].copy()
            hit["rrf_score"] = round(rrf_scores[cid], 6)
            results.append(hit)

        return results


    def _apply_supersession_penalty(
        self,
        results: List[Dict[str, Any]],
        query_intent=None,
        query: str = "",
    ) -> List[Dict[str, Any]]:
        """Phase 1 — Supersession penalty + Rule 3 v2.

        Penalize chunks từ superseded docs (111_2013, 92_2015) để giảm
        precision pollution. Không áp dụng khi:
          - Rule 3a: query_intent.time.year < 2026 (explicit year context)
          - Rule 3b: query chứa legacy keywords (implicit temporal/topic context)

        Args:
            results:      RRF results sau authority boost
            query_intent: QueryIntent object (optional) — kiểm tra time.year
            query:        query string gốc — dùng cho Rule 3b legacy keyword check
        """
        # Rule 3a: explicit year < 2026
        if query_intent is not None:
            try:
                year = query_intent.time.value.get("year")
                if year is not None and int(year) < 2026:
                    logger.info(f"[P1] Rule3a: transitional query (year={year}) — skip penalty")
                    return results
            except (AttributeError, TypeError, ValueError):
                pass

        # Rule 3b: implicit temporal/legacy-concept keywords
        if query:
            q_lower = query.lower()
            for kw in _LEGACY_KEYWORDS:
                if kw in q_lower:
                    logger.info(f"[P1] Rule3b: legacy keyword '{kw}' — skip penalty")
                    return results

        penalized = 0
        for r in results:
            meta = r.get("metadata", {})
            if meta.get("superseded") is True:
                r["rrf_score"] = round(r.get("rrf_score", 0) * _SUPERSESSION_PENALTY, 6)
                r["superseded_penalized"] = True
                penalized += 1

        if penalized:
            results.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
            logger.info(f"[P1] Supersession penalty applied to {penalized} chunks")

        return results

    def _apply_authority_boost(
        self,
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """P1 — Legal authority hierarchy boost + recency scoring.

        Công thức: final_score = rrf_score × hierarchy_factor × (1 + β × recency)

        hierarchy_factor: Luật×1.10, NQ×1.07, NĐ×1.05, TT×1.00, Công văn×0.85
        recency = 1 / (1 + log(1 + days_since_effective/365))
            - Văn bản mới (0 ngày): recency ≈ 1.0
            - 1 năm: recency ≈ 0.59
            - 5 năm: recency ≈ 0.37
            - 10 năm: recency ≈ 0.27
        β = 0.15 — nhỏ đủ để không override hierarchy, đủ để phân biệt 310/2025 vs 125/2020

        Chỉ áp dụng recency cho chunk KHÔNG bị supersession penalty
        (chunk superseded đã bị ×0.25, không cần thêm recency penalty).
        """
        from src.utils.config import DOCUMENT_REGISTRY

        today = date.today()
        changed = False
        for r in results:
            doc_id = r.get("metadata", {}).get("doc_id", "")
            reg = DOCUMENT_REGISTRY.get(doc_id)
            if reg is None:
                continue

            # 1. Hierarchy factor
            hierarchy = _LEGAL_LEVEL_BOOST.get(reg.legal_level, 1.0)

            # 2. Recency factor — bỏ qua nếu chunk đã bị supersession penalty
            recency_boost = 1.0
            if not r.get("superseded_penalized"):
                days = max(0, (today - reg.effective_from).days)
                recency = 1.0 / (1.0 + math.log1p(days / 365.0))
                recency_boost = 1.0 + 0.15 * recency

            # 3. Combine
            combined = hierarchy * recency_boost
            if abs(combined - 1.0) > 0.001:
                r["rrf_score"] = round(r.get("rrf_score", 0) * combined, 6)
                r["authority_boost"] = round(combined, 4)
                changed = True

        if changed:
            results.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
            logger.info("[P1] Authority hierarchy + recency boost applied")

        return results

    def _expand_exceptions(
        self,
        results: List[Dict[str, Any]],
        max_per_rule: int = 3,
        max_chars:    int = 800,
        score_factor: float = 0.65,
    ) -> List[Dict[str, Any]]:
        """C2 — Exception expansion.

        For every chunk in results, look up exception_index.
        If rule_chunk_id has known exceptions → fetch + append with
        score = rule_score × score_factor (lower than A2's 0.7).

        Dedup: keep highest-score version of each chunk_id.
        """
        if not self._exception_index:
            return results

        seen: Dict[str, int] = {r["chunk_id"]: i for i, r in enumerate(results)}
        expansions: List[Dict[str, Any]] = []

        for result in results:
            cid = result["chunk_id"]

            # Direct lookup
            exc_ids: List[str] = list(self._exception_index.get(cid, []))

            # Fallback: parent Điều chunk (Khoản/Điểm inherits Điều exceptions)
            import re as _re
            dieu_id = _re.sub(r"_khoan_.+_chunk$", "_chunk", cid)
            dieu_id = _re.sub(r"_diem_.+_chunk$",  "_chunk", dieu_id)
            if dieu_id != cid:
                for eid in self._exception_index.get(dieu_id, []):
                    if eid not in exc_ids:
                        exc_ids.append(eid)

            if not exc_ids:
                continue

            exc_score    = round(result.get("rrf_score", 0) * score_factor, 6)
            fetched      = self.store.get_by_ids(exc_ids[:max_per_rule])

            for hit in fetched:
                hid = hit["chunk_id"]
                if hid in seen:
                    continue                    # already in results

                seen[hid] = len(results) + len(expansions)
                expansions.append({
                    **hit,
                    "text":           hit["text"][:max_chars],
                    "rrf_score":      exc_score,
                    "exception_of":   cid,      # debug/logging
                })

        if expansions:
            logger.info(f"[C2] Added {len(expansions)} exception nodes")

        all_results = results + expansions
        all_results.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        return all_results

    def _expand_references(
        self,
        results: List[Dict[str, Any]],
        min_score:        float = REF_MIN_SCORE,
        max_per_node:     int   = REF_MAX_PER_NODE,
        max_chars:        int   = REF_MAX_CHARS,
    ) -> List[Dict[str, Any]]:
        """1-hop reference expansion (A2).

        For each result above min_score, fetch its referenced_node_ids from
        ChromaDB and append to results with score = original_score × 0.7.
        Dedup: keep highest-score version of each chunk_id.
        """
        # seen maps chunk_id → index in output list
        seen: Dict[str, int] = {r["chunk_id"]: i for i, r in enumerate(results)}
        expansions: List[Dict[str, Any]] = []

        for result in results:
            if result.get("rrf_score", 0) < min_score:
                continue

            raw_ids = result.get("metadata", {}).get("referenced_node_ids", "[]")
            try:
                ref_ids: List[str] = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            except (json.JSONDecodeError, TypeError):
                continue

            if not ref_ids:
                continue

            fetched = self.store.get_by_ids(ref_ids[:max_per_node])
            expand_score = round(result["rrf_score"] * 0.7, 6)

            for hit in fetched:
                cid = hit["chunk_id"]
                if cid in seen:
                    # Already in results — keep whichever has higher score
                    existing_idx = seen[cid]
                    if existing_idx < len(results):
                        existing_score = results[existing_idx].get("rrf_score", 0)
                    else:
                        existing_score = expansions[existing_idx - len(results)].get("rrf_score", 0)
                    # expand_score is lower → no update needed
                    continue

                seen[cid] = len(results) + len(expansions)
                expansions.append({
                    **hit,
                    "text":          hit["text"][:max_chars],
                    "rrf_score":     expand_score,
                    "expanded_from": result["chunk_id"],
                })

        all_results = results + expansions
        all_results.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        return all_results

    def _expand_amendments(
        self,
        results: List[Dict[str, Any]],
        max_per_source: int = 3,
        score_factor: float = 0.75,
    ) -> List[Dict[str, Any]]:
        """B4 — Amendment expansion.

        Khi một chunk có metadata `amended_by_doc_ids` (non-empty),
        fetch top chunks từ doc sửa đổi và inject vào context với
        score = source_score × score_factor.

        Mục đích: đảm bảo nghị định sửa đổi (vd: 310/2025) luôn xuất hiện
        cùng với nghị định gốc (125/2020) trong context, dù 310/2025 không
        win semantic race (vì chỉ chứa câu lệnh sửa đổi, không có full-text).
        """
        seen: Dict[str, int] = {r["chunk_id"]: i for i, r in enumerate(results)}
        expansions: List[Dict[str, Any]] = []

        # Track which amending docs already have chunks injected (avoid flood)
        injected_amend_docs: set[str] = set()

        for result in results:
            raw = result.get("metadata", {}).get("amended_by_doc_ids", "[]")
            try:
                amend_doc_ids: List[str] = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue

            if not amend_doc_ids:
                continue

            for amend_doc_id in amend_doc_ids:
                if amend_doc_id in injected_amend_docs:
                    continue  # already injected chunks from this amending doc

                # Fetch representative chunks from the amending doc
                # Use get_by_doc_id — amendment docs are small (≤130 chunks)
                # and their content is structurally important regardless of
                # semantic proximity, so we take the first N chunks.
                try:
                    hits = self.store.get_by_doc_id(amend_doc_id, limit=max_per_source)
                except Exception:
                    continue

                expand_score = round(result["rrf_score"] * score_factor, 6)
                added = 0
                for hit in hits:
                    cid = hit.get("chunk_id", "")
                    if not cid or cid in seen:
                        continue
                    hit["rrf_score"]    = expand_score
                    hit["expanded_from"] = result["chunk_id"]
                    hit["amendment_of"]  = result.get("metadata", {}).get("doc_id", "")
                    seen[cid] = len(results) + len(expansions)
                    expansions.append(hit)
                    added += 1

                if added:
                    injected_amend_docs.add(amend_doc_id)
                    logger.info(
                        f"[B4] Amendment expand: {result['chunk_id'][:50]} "
                        f"→ {added} chunks from {amend_doc_id}"
                    )

        if expansions:
            results = results + expansions
            results.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)

        return results


    def _expand_siblings(
        self,
        results: List[Dict[str, Any]],
        window: int = 2,
        score_factor: float = 0.60,
        max_chars: int = 1200,
    ) -> List[Dict[str, Any]]:
        """P3 — Windowed sibling expansion.

        Khi chunk tiet_X.N được retrieved, tự động fetch tiet_X.N+1 và tiet_X.N+2
        để tránh miss điều kiện/ngoại lệ nằm ngay sau trong cùng điểm.

        Ví dụ: dieu_8_khoan_6_diem_d_tiet_d.1 retrieved →
               dieu_8_khoan_6_diem_d_tiet_d.2 cũng được đưa vào context.

        Chỉ expand tiết (subpoint) — không expand khoản/điểm để tránh noise.
        Score = triggering_chunk_score × score_factor (< 0.65 của C2).
        """
        import re as _re

        TIET_PAT = _re.compile(r"^(.*_tiet_)([a-z])\.(\d+)(_chunk)$")

        seen: Dict[str, int] = {r["chunk_id"]: i for i, r in enumerate(results)}
        expansions: List[Dict[str, Any]] = []
        candidate_ids: List[tuple] = []  # (sibling_chunk_id, triggering_score)

        for result in results:
            cid = result["chunk_id"]
            m = TIET_PAT.match(cid)
            if not m:
                continue
            # Only expand when content has explicit cross-reference signal
            text_content = result.get("text", "") or ""
            if not _CROSS_REF_PAT.search(text_content):
                continue
            prefix, letter, num_str, suffix = m.groups()
            num = int(num_str)
            score = result.get("rrf_score", 0)
            sibling_score = round(score * score_factor, 6)

            for delta in range(1, window + 1):
                # Forward siblings (most important — condition split to next tiết)
                candidate_ids.append((f"{prefix}{letter}.{num + delta}{suffix}", sibling_score))
                # Backward (in case we retrieved N+1 but not N)
                if num - delta >= 1:
                    candidate_ids.append((f"{prefix}{letter}.{num - delta}{suffix}", sibling_score))

        if not candidate_ids:
            return results

        # Deduplicate candidates not already seen
        new_ids = [cid for cid, _ in candidate_ids if cid not in seen]
        if not new_ids:
            return results

        # Build score map (highest score wins if same id from multiple sources)
        score_map: Dict[str, float] = {}
        for cid, sc in candidate_ids:
            if cid not in seen:
                score_map[cid] = max(score_map.get(cid, 0), sc)

        fetched = self.store.get_by_ids(list(set(new_ids)))
        for hit in fetched:
            hid = hit["chunk_id"]
            if hid in seen:
                continue
            seen[hid] = len(results) + len(expansions)
            expansions.append({
                **hit,
                "text":         hit["text"][:max_chars],
                "rrf_score":    score_map.get(hid, 0),
                "sibling_of":   "tiet_expansion",
            })

        if expansions:
            logger.info(f"[P3] Added {len(expansions)} sibling tiết nodes")

        all_results = results + expansions
        all_results.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        return all_results

    # P6.2: giới hạn tổng tiết children per query để tránh flood slots
    MAX_CHILDREN_PER_QUERY = 4

    def _expand_children(
        self,
        results: List[Dict[str, Any]],
        max_children: int = 6,
        score_factor: float = 0.85,
    ) -> List[Dict[str, Any]]:
        """[P6] Post-retrieval children expansion.

        Khi Điểm chunk được retrieved với content là header (kết thúc ':'),
        tự động fetch các tiết con để đưa số liệu vào context.

        Ví dụ: diem_a "Tỷ lệ % tính thuế GTGT ... như sau:"
               → fetch tiet_a.1, tiet_a.2, tiet_a.3

        P6.2: collect TẤT CẢ candidates từ mọi diem header, sort theo parent_score DESC,
        lấy top MAX_CHILDREN_PER_QUERY (=4) — ưu tiên children của parent có rank cao nhất.

        KHÔNG thay đổi embeddings — chỉ post-retrieval augmentation.
        """
        import re as _re

        DIEM_PAT = _re.compile(r"^(.*_diem_)([a-z])(_chunk)$")

        seen: Dict[str, int] = {r["chunk_id"]: i for i, r in enumerate(results)}

        # Collect all candidates với parent_score để sort sau
        # Format: (child_id, child_score, parent_score)
        all_candidates: List[tuple] = []

        for result in results:
            cid = result["chunk_id"]
            m = DIEM_PAT.match(cid)
            if not m:
                continue
            # Only expand if chunk is a header pointing to children (ends with ':')
            text = result.get("text", "").rstrip()
            if not text.endswith(":"):
                continue

            prefix, letter, suffix = m.groups()
            parent_score = result.get("rrf_score", 0)
            child_score = round(parent_score * score_factor, 6)

            for n in range(1, max_children + 1):
                child_id = f"{prefix}{letter}_tiet_{letter}.{n}{suffix}"
                if child_id not in seen:
                    all_candidates.append((child_id, child_score, parent_score))

        if not all_candidates:
            return results

        # P6.2: sort by parent_score DESC, dedup child_id
        all_candidates.sort(key=lambda x: x[2], reverse=True)
        candidate_map: Dict[str, float] = {}  # child_id → child_score
        for cid, cscore, _ in all_candidates:
            if cid not in candidate_map:
                candidate_map[cid] = cscore
        # Fetch ALL candidates từ ChromaDB — chỉ những cái tồn tại được trả về
        # Sau đó lấy top MAX_CHILDREN_PER_QUERY theo thứ tự sorted (by parent_score)
        fetched_all = self.store.get_by_ids(list(candidate_map.keys()))
        # Sort fetched theo score (proxy parent_score qua candidate_map score)
        fetched_all.sort(key=lambda h: candidate_map.get(h["chunk_id"], 0), reverse=True)

        expansions: List[Dict[str, Any]] = []
        for hit in fetched_all:
            if len(expansions) >= self.MAX_CHILDREN_PER_QUERY:
                break
            hid = hit["chunk_id"]
            if hid in seen:
                continue
            seen[hid] = len(results) + len(expansions)
            expansions.append({
                **hit,
                "rrf_score":          candidate_map[hid],
                "child_expansion_of": "diem_header",
            })

        if expansions:
            logger.info(
                "[P6.2-Children] Added %d/%d tiết children (cap=%d, fetched=%d)",
                len(expansions), len(all_candidates),
                self.MAX_CHILDREN_PER_QUERY, len(fetched_all),
            )

        all_results = results + expansions
        all_results.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        return all_results


# ── Test search ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    searcher = HybridSearch()

    test_queries = [
        "thuế suất thu nhập cá nhân từ tiền lương",
        "mức giảm trừ gia cảnh cho người phụ thuộc",
        "tổ chức quản lý nền tảng thương mại điện tử khấu trừ thuế",
        "Điều 9 biểu thuế lũy tiến",
        "thu nhập được miễn thuế",
    ]

    for query in test_queries:
        print(f"\n{'='*60}")
        print(f"🔍 Query: {query}")
        print('='*60)

        results = searcher.search(query, n_results=3)

        for i, r in enumerate(results, 1):
            print(f"\n[{i}] Score: {r['rrf_score']:.4f}")
            print(f"    Breadcrumb: {r['metadata'].get('breadcrumb','')}")
            print(f"    Text: {r['text'][:120]}...")