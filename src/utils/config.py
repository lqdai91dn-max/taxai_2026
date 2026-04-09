"""
TaxAI 2026 - Central Configuration
Complete configuration with Gemini 2.5 Flash/Pro, intelligent routing, and monitoring
"""

from pathlib import Path
from dotenv import load_dotenv
import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Optional, List, Dict, TypedDict, Any
import json

# Load environment variables
load_dotenv()


# ==========================================
# SYSTEM CONFIGURATION
# ==========================================

class Config:
    """Central system configuration"""

    # -------------------------------
    # SYSTEM MODE & SAFETY FLAGS
    # -------------------------------
    SYSTEM_MODE = os.getenv("SYSTEM_MODE", "development")  # development | production
    
    # CRITICAL SAFETY FLAGS - DO NOT MODIFY
    STRICT_LEGAL_MODE = True  # Enforce legal accuracy requirements
    ALLOW_LLM_CALCULATION = False  # MUST remain False - use deterministic engine only
    REQUIRE_CITATION = True  # Every answer MUST have source citation
    ENABLE_HALLUCINATION_GUARD = True  # Verify answers against source documents
    
    # Quality thresholds
    MIN_CONFIDENCE_THRESHOLD = 0.75  # Minimum confidence to provide answer
    MIN_RETRIEVAL_SCORE = 0.70  # Minimum relevance score for retrieved chunks
    MAX_UNCERTAINTY_FLAG = 3  # Max number of uncertainty flags before escalation

    # -------------------------------
    # API KEYS
    # -------------------------------
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    
    if not GOOGLE_API_KEY and SYSTEM_MODE == "production":
        raise ValueError("GOOGLE_API_KEY must be set in production mode")

    # -------------------------------
    # PATH CONFIGURATION
    # -------------------------------
    PROJECT_ROOT = Path(__file__).parent.parent.parent
    DATA_DIR = PROJECT_ROOT / "data"
    RAW_DIR = DATA_DIR / "raw"
    PARSED_DIR = DATA_DIR / "parsed" / "documents"
    PROCESSED_DIR = DATA_DIR / "processed"
    KB_DIR = DATA_DIR / "knowledge_base"
    LOG_DIR = PROJECT_ROOT / "logs"
    
    # Subdirectories in processed/
    CHUNKS_DIR = PROCESSED_DIR / "chunks"
    METADATA_DIR = PROCESSED_DIR / "metadata"
    NUMERIC_DIR = PROCESSED_DIR / "numeric_data"
    EMBEDDINGS_DIR = PROCESSED_DIR / "embeddings"
    
    # Vector database
    VECTOR_DB_DIR = DATA_DIR / "vector_db"
    CHROMA_PERSIST_DIR = VECTOR_DB_DIR / "chroma"
    
    # Cache
    CACHE_DIR = DATA_DIR / "cache"

    # Create all required directories
    for dir_path in [
        DATA_DIR, RAW_DIR, PARSED_DIR, PROCESSED_DIR, KB_DIR, LOG_DIR,
        CHUNKS_DIR, METADATA_DIR, NUMERIC_DIR, EMBEDDINGS_DIR,
        VECTOR_DB_DIR, CHROMA_PERSIST_DIR, CACHE_DIR
    ]:
        dir_path.mkdir(parents=True, exist_ok=True)

    # -------------------------------
    # PARSING SETTINGS
    # -------------------------------
    PARSE_VERBOSE = True  # Print detailed parsing logs
    PARSE_SAVE_INTERMEDIATE = True  # Save intermediate parsing results
    PARSE_VALIDATE = True  # Validate parsed documents
    
    # Regex timeout (seconds) - prevent infinite loops
    REGEX_TIMEOUT = 5

    # -------------------------------
    # LLM CONFIGURATION - GEMINI 2.5
    # -------------------------------
    
    # Gemini 3 models (paid tier 1)
    GEMINI_MODEL_DEFAULT = "gemini-3-flash-preview"  # Fast, cheap, good enough for 85% queries
    GEMINI_MODEL_PREMIUM = "gemini-3-pro-preview"    # Slow, expensive, best reasoning
    
    # Enable intelligent routing
    ENABLE_INTELLIGENT_ROUTING = True
    
    # Routing strategy
    ROUTING_STRATEGY = "upfront"  # "upfront" | "progressive" | "query_type"
    
    # Routing thresholds (for upfront strategy)
    ROUTING_COMPLEXITY_THRESHOLD = 0.7  # >0.7 = complex query → Pro
    ROUTING_CHUNK_COUNT_THRESHOLD = 5   # >5 chunks → Pro
    ROUTING_RETRIEVAL_SCORE_THRESHOLD = 0.78  # <0.78 = uncertain → Pro
    ROUTING_MULTI_DOC_THRESHOLD = 3  # >3 different docs → Pro
    
    # Model-specific settings
    GEMINI_SETTINGS = {
        "gemini-2.5-flash": {
            "temperature": 0.0,  # Deterministic for legal answers
            "max_output_tokens": 8192,  # 2.5 Flash supports up to 8K
            "top_p": 0.95,
            "top_k": 40,
        },
        "gemini-2.5-pro": {
            "temperature": 0.0,
            "max_output_tokens": 8192,  # 2.5 Pro supports up to 8K
            "top_p": 0.95,
            "top_k": 40,
        }
    }
    
    # Safety settings (Gemini API)
    GEMINI_SAFETY_SETTINGS = {
        "HARM_CATEGORY_HARASSMENT": "BLOCK_NONE",
        "HARM_CATEGORY_HATE_SPEECH": "BLOCK_NONE",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE",
        "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_NONE",
    }
    
    # Fallback strategy
    ENABLE_FALLBACK_TO_PRO = True  # If Flash fails, retry with Pro
    MAX_FLASH_RETRIES = 1  # Retry once before falling back
    
    # Budget management
    DAILY_PRO_QUERY_LIMIT = 100  # Limit Pro queries for cost control (0 = unlimited)
    WARN_WHEN_PRO_USAGE_HIGH = True
    PRO_USAGE_WARN_THRESHOLD = 0.20  # Warn when Pro usage >20%
    
    # Rate limiting (respect API limits)
    FLASH_RPM_LIMIT = 14  # Requests per minute (under official 15 to be safe)
    PRO_RPM_LIMIT = 2
    ENABLE_RATE_LIMITING = True
    
    # Performance tracking
    TRACK_MODEL_PERFORMANCE = True
    LOG_MODEL_SELECTION = True  # Log which model was used for each query
    MODEL_STATS_FILE = LOG_DIR / "model_usage_stats.json"

    # -------------------------------
    # EMBEDDING CONFIGURATION
    # -------------------------------
    EMBEDDING_MODEL = "text-embedding-004"  # Google's multilingual model
    EMBEDDING_DIMENSION = 768
    EMBEDDING_BATCH_SIZE = 100  # Process embeddings in batches
    
    # Alternative for offline: "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    # -------------------------------
    # RETRIEVAL CONFIGURATION
    # -------------------------------
    # Hybrid retrieval
    RETRIEVAL_TOP_K = 10  # Initial retrieval
    RETRIEVAL_SCORE_THRESHOLD = 0.70  # Minimum relevance
    
    # Re-ranking
    RERANK_TOP_K = 5  # After re-ranking
    RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    
    # Search strategy
    HYBRID_SEARCH_ALPHA = 0.5  # 0.5 = equal weight BM25 and vector
    # 0.0 = pure BM25, 1.0 = pure vector
    
    # Metadata filtering
    ENABLE_METADATA_FILTER = True
    FILTER_BY_EFFECTIVE_DATE = True
    FILTER_BY_TAX_SCOPE = True

    # -------------------------------
    # CACHING CONFIGURATION
    # -------------------------------
    ENABLE_SEMANTIC_CACHE = True
    CACHE_SIMILARITY_THRESHOLD = 0.95  # Query similarity for cache hit
    CACHE_TTL_DAYS = 30  # Cache time-to-live
    CACHE_MAX_SIZE_MB = 500  # Max cache size

    # -------------------------------
    # CHUNKING CONFIGURATION
    # -------------------------------
    # Legal-aware chunking
    CHUNK_STRATEGY = "article"  # article | clause | adaptive
    CHUNK_MIN_LENGTH = 100  # Minimum characters
    CHUNK_MAX_LENGTH = 2000  # Maximum characters
    CHUNK_OVERLAP = 200  # Overlap between chunks
    
    # Context preservation
    INCLUDE_CHAPTER_CONTEXT = True
    INCLUDE_ARTICLE_TITLE = True
    INCLUDE_RELATED_DEFINITIONS = True

    # -------------------------------
    # QUERY ROUTING KEYWORDS
    # -------------------------------
    # These help classify query complexity
    
    CALCULATION_KEYWORDS = [
        "tính", "bao nhiêu", "phải nộp", "mức thuế",
        "doanh thu", "thu nhập", "giảm trừ"
    ]
    
    EXPLANATION_KEYWORDS = [
        "giải thích", "tại sao", "như thế nào", "ví dụ",
        "nghĩa là gì", "là gì", "khác nhau"
    ]
    
    PROCEDURE_KEYWORDS = [
        "thủ tục", "hồ sơ", "đăng ký", "khai báo",
        "nộp", "quyết toán", "cách làm"
    ]
    
    COMPLEX_REASONING_KEYWORDS = [
        "so sánh", "phân tích", "đánh giá", "ưu nhược điểm",
        "khác biệt", "giống nhau", "tại sao", "vì sao",
        "tổng hợp", "toàn bộ", "tất cả các",
        "có thể", "nên", "trường hợp nào", "khi nào",
        "mâu thuẫn", "theo.*hay theo"
    ]
    
    SIMPLE_PATTERNS = [
        r"mức \w+ là bao nhiêu",
        r"(điều|khoản|điểm) \d+",
        r"hạn (nộp|khai)",
        r"ngưỡng doanh thu",
        r"thủ tục \w+",
        r"^(bao nhiêu|mấy|gì|ở đâu|khi nào|ai)",
    ]

    # -------------------------------
    # AUDIT & LOGGING
    # -------------------------------
    # Audit trail
    ENABLE_AUDIT_LOG = True
    AUDIT_LOG_DIR = LOG_DIR / "audit"
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    # Log levels
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # DEBUG | INFO | WARNING | ERROR
    LOG_ROTATION = "1 day"
    LOG_RETENTION = "30 days"
    
    # What to log
    LOG_QUERY_INPUT = True
    LOG_RETRIEVAL_RESULTS = True
    LOG_LLM_PROMPTS = False  # Be careful in production (data privacy)
    LOG_LLM_RESPONSES = True
    LOG_CONFIDENCE_SCORES = True

    # -------------------------------
    # VERIFICATION SETTINGS
    # -------------------------------
    # Post-generation verification
    ENABLE_CITATION_VERIFICATION = True  # Verify citations exist in source
    ENABLE_FACT_CONSISTENCY_CHECK = True  # Check facts match source
    ENABLE_COMPLETENESS_CHECK = True  # Check all conditions mentioned
    
    # Confidence scoring weights
    CONFIDENCE_WEIGHTS = {
        "retrieval_score": 0.3,
        "citation_validity": 0.3,
        "fact_consistency": 0.2,
        "llm_confidence": 0.2,
    }

    # -------------------------------
    # UI/UX SETTINGS
    # -------------------------------
    # Streamlit app settings
    APP_TITLE = "TaxAI 2026 - Trợ Lý Thuế Thông Minh"
    APP_ICON = "💼"
    SHOW_SOURCES = True
    SHOW_CONFIDENCE_SCORE = True
    SHOW_MODEL_USED = True  # Show which model answered
    MAX_CHAT_HISTORY = 50

    # -------------------------------
    # DEVELOPMENT SETTINGS
    # -------------------------------
    # Testing
    ENABLE_TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
    TEST_DATA_DIR = DATA_DIR / "test"
    
    # Debug
    DEBUG_MODE = SYSTEM_MODE == "development"
    SAVE_DEBUG_ARTIFACTS = DEBUG_MODE  # Save intermediate results for debugging


config = Config()


# ==========================================
# TAX CALCULATION CONSTANTS (2026)
# ==========================================

class TaxConstants:
    """Tax calculation constants for 2026"""
    
    # -------------------------------
    # GIẢM TRỪ GIA CẢNH (NQ 110/2025)
    # Hiệu lực: 01/07/2026
    # -------------------------------
    FAMILY_DEDUCTION_SELF = 11_000_000  # VND/tháng (bản thân)
    FAMILY_DEDUCTION_DEPENDENT = 4_400_000  # VND/tháng/người phụ thuộc
    
    # Annual equivalents
    FAMILY_DEDUCTION_SELF_ANNUAL = FAMILY_DEDUCTION_SELF * 12  # 132 triệu/năm
    FAMILY_DEDUCTION_DEPENDENT_ANNUAL = FAMILY_DEDUCTION_DEPENDENT * 12  # 52.8 triệu/năm
    
    # Previous values (for comparison/transition)
    FAMILY_DEDUCTION_SELF_OLD = 9_000_000  # Before 01/07/2026
    FAMILY_DEDUCTION_DEPENDENT_OLD = 3_600_000  # Before 01/07/2026

    # -------------------------------
    # BIỂU THUẾ LŨY TIẾN (Luật TNCN 2025)
    # Hiệu lực: 01/07/2026
    # -------------------------------
    PROGRESSIVE_TAX_BRACKETS = [
        {
            "bracket": 1,
            "min": 0,
            "max": 5_000_000,
            "rate": 0.05,
            "description": "Đến 5 triệu đồng"
        },
        {
            "bracket": 2,
            "min": 5_000_000,
            "max": 10_000_000,
            "rate": 0.10,
            "description": "Trên 5 triệu đến 10 triệu"
        },
        {
            "bracket": 3,
            "min": 10_000_000,
            "max": 18_000_000,
            "rate": 0.15,
            "description": "Trên 10 triệu đến 18 triệu"
        },
        {
            "bracket": 4,
            "min": 18_000_000,
            "max": 32_000_000,
            "rate": 0.20,
            "description": "Trên 18 triệu đến 32 triệu"
        },
        {
            "bracket": 5,
            "min": 32_000_000,
            "max": 52_000_000,
            "rate": 0.25,
            "description": "Trên 32 triệu đến 52 triệu"
        },
        {
            "bracket": 6,
            "min": 52_000_000,
            "max": 80_000_000,
            "rate": 0.30,
            "description": "Trên 52 triệu đến 80 triệu"
        },
        {
            "bracket": 7,
            "min": 80_000_000,
            "max": float('inf'),
            "rate": 0.35,
            "description": "Trên 80 triệu"
        },
    ]

    # -------------------------------
    # TỶ LỆ % THU NHẬP HỘ KINH DOANH
    # (Thông tư 152/2025/TT-BTC)
    # Hiệu lực: 01/01/2026
    # -------------------------------
    HOUSEHOLD_BUSINESS_INCOME_PERCENTAGE = {
        "commerce": {
            "rate": 0.30,
            "description": "Thương mại, mua bán hàng hóa",
            "code": "TM"
        },
        "service": {
            "rate": 0.20,
            "description": "Dịch vụ (nhà hàng, sửa chữa, ...)",
            "code": "DV"
        },
        "manufacturing": {
            "rate": 0.15,
            "description": "Sản xuất, chế biến",
            "code": "SX"
        },
        "other": {
            "rate": 0.20,
            "description": "Hoạt động khác",
            "code": "KH"
        },
    }

    # -------------------------------
    # NGƯỠNG DOANH THU (Luật GTGT 2025)
    # Hiệu lực: 01/01/2026
    # -------------------------------
    
    # Ngưỡng đăng ký thuế GTGT
    VAT_REGISTRATION_THRESHOLD = 100_000_000  # 100 triệu/năm
    
    # Ngưỡng doanh thu không chịu thuế (NQ 198/2025)
    REVENUE_THRESHOLD_NO_TAX = 100_000_000  # 100 triệu/năm
    
    # Ngưỡng buộc phải có hóa đơn điện tử
    EINVOICE_MANDATORY_THRESHOLD = 100_000_000  # 100 triệu/năm

    # -------------------------------
    # THUẾ SUẤT GTGT
    # -------------------------------
    VAT_RATES = {
        "standard": 0.10,  # 10% - Thuế suất chuẩn
        "reduced": 0.05,   # 5% - Thuế suất giảm
        "zero": 0.00,      # 0% - Hàng xuất khẩu
    }

    # -------------------------------
    # THỜI HẠN NỘP THUẾ & KHAI BÁO
    # -------------------------------
    
    # Thuế TNCN (theo tháng)
    PIT_MONTHLY_DEADLINE_DAY = 20  # Ngày 20 tháng sau
    
    # Quyết toán thuế TNCN (hàng năm)
    PIT_ANNUAL_DEADLINE_MONTH = 3  # Tháng 3
    PIT_ANNUAL_DEADLINE_DAY = 31   # Ngày 31/3
    
    # Thuế GTGT (theo tháng/quý)
    VAT_MONTHLY_DEADLINE_DAY = 20
    VAT_QUARTERLY_DEADLINE_DAY = 30  # Ngày 30 tháng đầu quý sau

    # -------------------------------
    # MỨC PHẠT (Nghị định 310/2025)
    # Hiệu lực: 16/01/2026
    # -------------------------------
    PENALTY_LATE_FILING_MIN = 2_000_000  # 2 triệu - Nộp hồ sơ chậm
    PENALTY_LATE_FILING_MAX = 25_000_000  # 25 triệu
    
    PENALTY_LATE_PAYMENT_RATE = 0.03  # 0.03%/ngày - Nộp thuế chậm
    # (Tính trên số tiền thuế chậm nộp)
    
    PENALTY_TAX_EVASION_RATE_MIN = 1.0  # 1 lần số thuế trốn
    PENALTY_TAX_EVASION_RATE_MAX = 3.0  # 3 lần số thuế trốn

    # -------------------------------
    # THỜI ĐIỂM CHUYỂN ĐỔI QUAN TRỌNG
    # -------------------------------
    TRANSITION_DATES = {
        "abolish_presumptive_tax": date(2026, 1, 1),  # Bỏ thuế khoán
        "abolish_license_fee": date(2026, 1, 1),      # Bỏ lệ phí môn bài
        "new_pit_law": date(2026, 7, 1),              # Luật TNCN mới
        "new_gtgc": date(2026, 7, 1),                 # Mức GTGC mới
        "new_vat_law": date(2026, 1, 1),              # Luật GTGT mới
    }


tax_constants = TaxConstants()


# ==========================================
# MODEL SELECTION & ROUTING
# ==========================================

@dataclass
class RoutingDecision:
    """Result of routing decision"""
    model: str  # Full model name (e.g., "gemini-2.5-flash")
    model_type: str  # "flash" or "pro"
    reason: str
    complexity_score: float
    confidence: float
    fallback_available: bool = True
    factors: Dict = field(default_factory=dict)


class IntelligentModelRouter:
    """
    Intelligent router to select between Flash (fast/cheap) and Pro (smart/expensive)
    Uses upfront decision strategy based on query analysis + retrieval context
    """
    
    def __init__(self):
        self.flash_model = config.GEMINI_MODEL_DEFAULT
        self.pro_model = config.GEMINI_MODEL_PREMIUM
        
        # Track usage
        self.pro_queries_today = 0
        self.total_queries_today = 0
        
        # Load previous stats if exists
        self._load_daily_stats()
    
    def route(
        self,
        query: str,
        retrieval_context: Optional[Dict] = None
    ) -> RoutingDecision:
        """
        Main routing logic - Upfront decision strategy
        
        Args:
            query: User query string
            retrieval_context: {
                "chunks": List[Dict],
                "scores": List[float],
                "avg_score": float,
                "unique_docs": int,
                "has_conflicts": bool
            }
            
        Returns:
            RoutingDecision
        """
        
        if not config.ENABLE_INTELLIGENT_ROUTING:
            return RoutingDecision(
                model=self.flash_model,
                model_type="flash",
                reason="Intelligent routing disabled",
                complexity_score=0.0,
                confidence=1.0
            )
        
        self.total_queries_today += 1
        
        # Check Pro budget limit
        if self._is_pro_budget_exceeded():
            return RoutingDecision(
                model=self.flash_model,
                model_type="flash",
                reason=f"Pro budget limit reached ({self.pro_queries_today}/{config.DAILY_PRO_QUERY_LIMIT})",
                complexity_score=0.0,
                confidence=0.8,
                fallback_available=False
            )
        
        # Analyze query complexity
        query_complexity = self._assess_query_complexity(query)
        
        # Analyze retrieval context (if available)
        retrieval_factors = self._analyze_retrieval_context(retrieval_context)
        
        # Calculate overall complexity score
        factors = {
            "query_complexity": query_complexity,
            **retrieval_factors
        }
        
        complexity_score = self._calculate_complexity_score(factors)
        
        # Make decision
        if complexity_score > config.ROUTING_COMPLEXITY_THRESHOLD:
            # Use PRO
            self.pro_queries_today += 1
            
            return RoutingDecision(
                model=self.pro_model,
                model_type="pro",
                reason=self._explain_pro_decision(factors),
                complexity_score=complexity_score,
                confidence=complexity_score,
                factors=factors
            )
        else:
            # Use FLASH
            return RoutingDecision(
                model=self.flash_model,
                model_type="flash",
                reason="Standard query, Flash sufficient",
                complexity_score=complexity_score,
                confidence=1.0 - complexity_score,
                factors=factors
            )
    
    def _assess_query_complexity(self, query: str) -> float:
        """
        Assess query complexity (0.0 = simple, 1.0 = very complex)
        """
        import re
        
        query_lower = query.lower()
        
        # Check simple patterns first
        for pattern in config.SIMPLE_PATTERNS:
            if re.search(pattern, query_lower):
                return 0.2  # Definitely simple
        
        # Count complex indicators
        complex_count = sum(
            1 for keyword in config.COMPLEX_REASONING_KEYWORDS
            if keyword in query_lower or re.search(keyword, query_lower)
        )
        
        # Normalize (assume max 3 complex keywords = definitely complex)
        complexity = min(complex_count / 3, 1.0)
        
        # Query length factor (very long = likely complex)
        if len(query) > 200:
            complexity += 0.2
        
        # Multiple questions indicator
        question_marks = query.count('?')
        if question_marks > 2:
            complexity += 0.1
        
        return min(complexity, 1.0)
    
    def _analyze_retrieval_context(self, retrieval_context: Optional[Dict]) -> Dict:
        """
        Analyze retrieval context to determine complexity
        """
        if not retrieval_context:
            return {
                "chunk_count": 0,
                "retrieval_quality": 1.0,
                "multi_document": 0,
                "has_conflicts": False
            }
        
        chunks = retrieval_context.get("chunks", [])
        scores = retrieval_context.get("scores", [])
        
        return {
            "chunk_count": len(chunks),
            "retrieval_quality": retrieval_context.get("avg_score", 1.0),
            "multi_document": retrieval_context.get("unique_docs", 0),
            "has_conflicts": retrieval_context.get("has_conflicts", False)
        }
    
    def _calculate_complexity_score(self, factors: Dict) -> float:
        """
        Calculate overall complexity score (0.0-1.0)
        """
        weights = {
            "query_complexity": 0.40,  # Most important
            "chunk_count": 0.20,
            "retrieval_quality": 0.20,
            "multi_document": 0.10,
            "has_conflicts": 0.10,
        }
        
        # Normalize factors
        normalized = {
            "query_complexity": factors["query_complexity"],
            
            "chunk_count": min(
                factors["chunk_count"] / config.ROUTING_CHUNK_COUNT_THRESHOLD, 
                1.0
            ),
            
            # Low retrieval score = high complexity (invert)
            "retrieval_quality": max(
                1.0 - factors["retrieval_quality"],
                0.0
            ),
            
            "multi_document": min(
                factors["multi_document"] / config.ROUTING_MULTI_DOC_THRESHOLD,
                1.0
            ),
            
            "has_conflicts": 1.0 if factors["has_conflicts"] else 0.0,
        }
        
        # Weighted sum
        score = sum(
            normalized[factor] * weights[factor]
            for factor in weights
        )
        
        return score
    
    def _explain_pro_decision(self, factors: Dict) -> str:
        """Generate human-readable explanation"""
        reasons = []
        
        if factors["query_complexity"] > 0.6:
            reasons.append("complex reasoning required")
        
        if factors["chunk_count"] > config.ROUTING_CHUNK_COUNT_THRESHOLD:
            reasons.append(f"{factors['chunk_count']} sources")
        
        if factors["retrieval_quality"] < config.ROUTING_RETRIEVAL_SCORE_THRESHOLD:
            reasons.append(f"uncertain retrieval ({factors['retrieval_quality']:.2f})")
        
        if factors["multi_document"] > 2:
            reasons.append(f"{factors['multi_document']} documents")
        
        if factors["has_conflicts"]:
            reasons.append("conflicting sources")
        
        return "Using Pro: " + ", ".join(reasons) if reasons else "Using Pro"
    
    def _is_pro_budget_exceeded(self) -> bool:
        """Check if Pro budget exceeded"""
        if config.DAILY_PRO_QUERY_LIMIT == 0:  # 0 = unlimited
            return False
        return self.pro_queries_today >= config.DAILY_PRO_QUERY_LIMIT
    
    def get_usage_stats(self) -> Dict:
        """Get current usage statistics"""
        pro_pct = (
            self.pro_queries_today / self.total_queries_today * 100
            if self.total_queries_today > 0 else 0
        )
        
        return {
            "date": date.today().isoformat(),
            "total_queries": self.total_queries_today,
            "flash_queries": self.total_queries_today - self.pro_queries_today,
            "pro_queries": self.pro_queries_today,
            "pro_percentage": round(pro_pct, 1),
            "pro_budget_remaining": max(
                0,
                config.DAILY_PRO_QUERY_LIMIT - self.pro_queries_today
            ) if config.DAILY_PRO_QUERY_LIMIT > 0 else "unlimited",
        }
    
    def _load_daily_stats(self):
        """Load today's stats from file"""
        if config.MODEL_STATS_FILE.exists():
            try:
                with open(config.MODEL_STATS_FILE, 'r') as f:
                    data = json.load(f)
                
                # Check if it's today
                if data.get("date") == date.today().isoformat():
                    self.total_queries_today = data.get("total_queries", 0)
                    self.pro_queries_today = data.get("pro_queries", 0)
            except:
                pass
    
    def save_daily_stats(self):
        """Save today's stats to file"""
        try:
            with open(config.MODEL_STATS_FILE, 'w') as f:
                json.dump(self.get_usage_stats(), f, indent=2)
        except Exception as e:
            from .logger import logger
            logger.error(f"Failed to save model stats: {e}")


# Global router instance
model_router = IntelligentModelRouter()


# ==========================================
# TYPE DEFINITIONS
# ==========================================

class TaxScope(TypedDict, total=False):
    """Standardized tax scope structure for metadata"""
    tax_type: List[str]  # ["PIT", "VAT", "CIT"]
    income_type: List[str]  # ["salary", "business", "capital", "transfer"]
    taxpayer_type: List[str]  # ["individual", "household", "enterprise"]
    regime: List[str]  # ["progressive", "flat", "percentage"]
    affects: List[str]  # ["calculation", "deduction", "exemption", "penalty", "filing"]
    activity_type: List[str]  # ["commerce", "service", "manufacturing", "ecommerce"]


# ==========================================
# DATA MODELS
# ==========================================

@dataclass
class Point:
    """Điểm (a, b, c, d, ...)"""
    letter: str
    content: str
    subpoints: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Clause:
    """Khoản (1, 2, 3, ...)"""
    number: str
    content: str
    points: List[Point] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "number": self.number,
            "content": self.content,
            "points": [p.to_dict() for p in self.points]
        }


@dataclass
class Article:
    """Điều luật"""
    number: str
    title: str
    content: str
    clauses: List[Clause] = field(default_factory=list)
    
    # Hierarchy context
    chapter: Optional[str] = None
    chapter_title: Optional[str] = None
    
    # Metadata
    effective_date: Optional[date] = None  # Ngày hiệu lực riêng (nếu có)
    supersedes: List[str] = field(default_factory=list)  # Thay thế điều nào
    amended_by: List[str] = field(default_factory=list)  # Bị sửa bởi điều nào
    
    # Tax-specific metadata
    tax_scope: Optional[TaxScope] = None
    numeric_data: Dict = field(default_factory=dict)  # Extracted numbers/rates
    
    def to_dict(self) -> Dict:
        result = {
            "number": self.number,
            "title": self.title,
            "content": self.content,
            "clauses": [c.to_dict() for c in self.clauses],
            "chapter": self.chapter,
            "chapter_title": self.chapter_title,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "supersedes": self.supersedes,
            "amended_by": self.amended_by,
            "tax_scope": self.tax_scope,
            "numeric_data": self.numeric_data,
        }
        return result


@dataclass
class Appendix:
    """Phụ lục (bảng biểu, biểu mẫu, ...)"""
    number: str
    title: str
    content: str
    type: str = "text"  # text | table | form | image
    structured_data: Optional[Dict] = None  # For tables/forms
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class LegalDocument:
    """Văn bản pháp luật hoàn chỉnh"""
    
    # ============ IDENTITY ============
    doc_id: str
    doc_type: str  # law | resolution | decree | circular
    number: str
    title: str
    
    # ============ ISSUER ============
    issued_by: str
    issued_date: date
    
    # ============ EFFECTIVENESS ============
    effective_from: date
    effective_to: Optional[date] = None  # Ngày hết hiệu lực
    
    # ============ LEGAL HIERARCHY ============
    legal_level: int = 1  # 1=Law, 2=Resolution, 3=Decree, 4=Circular
    status: str = "active"  # active | superseded | expired
    
    # ============ RELATIONSHIPS ============
    supersedes: List[str] = field(default_factory=list)  # Thay thế văn bản nào
    amended_by: List[str] = field(default_factory=list)  # Bị sửa bởi văn bản nào
    implements: List[str] = field(default_factory=list)  # Hướng dẫn văn bản nào
    amends: List[str] = field(default_factory=list)  # Sửa đổi văn bản nào
    
    # ============ CONTENT STRUCTURE ============
    preamble: str = ""  # Phần mở đầu
    articles: List[Article] = field(default_factory=list)
    appendices: List[Appendix] = field(default_factory=list)
    
    # ============ SOURCE ============
    source_file: str = ""
    
    # ============ CATEGORIZATION ============
    category: str = "General"  # TNCN | GTGT | Policy | Accounting | Penalty | TMDT
    tax_scope: TaxScope = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    # ============ SCOPE OF APPLICATION (P1) ============
    # "general"         — áp dụng cho mọi đối tượng (Luật, NĐ, TT, Công văn hướng dẫn chung)
    # "specific_entity" — chỉ áp dụng cho 1 doanh nghiệp/cá nhân cụ thể
    #                     (Công văn trả lời của CQT cho 1 taxpayer riêng)
    # Rule: khi câu hỏi mang tính nguyên tắc chung → không dùng specific_entity docs
    scope_of_application: str = "general"
    
    # ============ PROCESSING METADATA ============
    parsed_date: str = field(default_factory=lambda: datetime.now().isoformat())
    version: str = "1.0"
    
    # ============ METHODS ============
    
    def is_effective_on(self, query_date: date) -> bool:
        """Check if document is effective on a given date"""
        if query_date < self.effective_from:
            return False
        if self.effective_to and query_date > self.effective_to:
            return False
        return True
    
    def get_article(self, article_number: str) -> Optional[Article]:
        """Get article by number"""
        for article in self.articles:
            if article.number == article_number:
                return article
        return None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type,
            "number": self.number,
            "title": self.title,
            "issued_by": self.issued_by,
            "issued_date": self.issued_date.isoformat(),
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "legal_level": self.legal_level,
            "status": self.status,
            "supersedes": self.supersedes,
            "amended_by": self.amended_by,
            "implements": self.implements,
            "amends": self.amends,
            "preamble": self.preamble,
            "articles": [a.to_dict() for a in self.articles],
            "appendices": [a.to_dict() for a in self.appendices],
            "source_file": self.source_file,
            "category": self.category,
            "tax_scope": self.tax_scope,
            "tags": self.tags,
            "parsed_date": self.parsed_date,
            "version": self.version,
        }
    
    def save_json(self, filepath: Path):
        """Save to JSON file"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'LegalDocument':
        """Create from dictionary (JSON deserialization)"""
        data = data.copy()
        
        # Convert date strings to date objects
        data["issued_date"] = date.fromisoformat(data["issued_date"])
        data["effective_from"] = date.fromisoformat(data["effective_from"])
        if data.get("effective_to"):
            data["effective_to"] = date.fromisoformat(data["effective_to"])
        
        # Reconstruct articles
        articles = []
        for art_data in data.get("articles", []):
            clauses = []
            for clause_data in art_data.get("clauses", []):
                points = []
                for point_data in clause_data.get("points", []):
                    points.append(Point(**point_data))
                clauses.append(Clause(
                    number=clause_data["number"],
                    content=clause_data["content"],
                    points=points
                ))
            
            effective_date = None
            if art_data.get("effective_date"):
                effective_date = date.fromisoformat(art_data["effective_date"])
            
            articles.append(Article(
                number=art_data["number"],
                title=art_data["title"],
                content=art_data["content"],
                clauses=clauses,
                chapter=art_data.get("chapter"),
                chapter_title=art_data.get("chapter_title"),
                effective_date=effective_date,
                supersedes=art_data.get("supersedes", []),
                amended_by=art_data.get("amended_by", []),
                tax_scope=art_data.get("tax_scope"),
                numeric_data=art_data.get("numeric_data", {}),
            ))
        
        # Reconstruct appendices
        appendices = [Appendix(**app_data) for app_data in data.get("appendices", [])]
        
        # Remove nested structures from data
        data.pop("articles", None)
        data.pop("appendices", None)
        
        return cls(
            **data,
            articles=articles,
            appendices=appendices
        )
    
    @classmethod
    def load_json(cls, filepath: Path) -> 'LegalDocument':
        """Load from JSON file"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)


# ==========================================
# LEGAL HIERARCHY & AUTHORITY
# ==========================================

LEGAL_AUTHORITY_RANK = {
    "law": 1,          # Highest authority
    "resolution": 2,
    "decree": 3,
    "circular": 4,     # Lowest authority
}


class LegalHierarchyResolver:
    """Resolve conflicts between documents based on legal hierarchy"""
    
    @staticmethod
    def resolve_conflict(docs: List[LegalDocument]) -> LegalDocument:
        """
        When multiple docs have conflicting rules, pick the highest authority
        """
        if not docs:
            return None
        
        # Sort by legal_level (lower = higher authority)
        sorted_docs = sorted(docs, key=lambda d: d.legal_level)
        
        return sorted_docs[0]
    
    @staticmethod
    def is_higher_authority(doc1: LegalDocument, doc2: LegalDocument) -> bool:
        """Check if doc1 has higher legal authority than doc2"""
        return doc1.legal_level < doc2.legal_level
    
    @staticmethod
    def get_applicable_documents(
        docs: List[LegalDocument], 
        query_date: date
    ) -> List[LegalDocument]:
        """Filter documents that are effective on query_date"""
        return [d for d in docs if d.is_effective_on(query_date)]


# ==========================================
# DOCUMENT REGISTRY (ALL 9 DOCUMENTS)
# ==========================================

DOCUMENT_REGISTRY: Dict[str, LegalDocument] = {

    # ==============================
    # LUẬT THUẾ TNCN 2025
    # ==============================
    "109_2025_QH15": LegalDocument(
        doc_id="luat_thue_tncn_2025",
        doc_type="law",
        number="109/2025/QH15",
        title="Luật Thuế thu nhập cá nhân",
        issued_by="Quốc hội",
        issued_date=date(2025, 11, 30),
        effective_from=date(2026, 7, 1),
        legal_level=1,
        supersedes=["luat_04_2007_qh12"],
        category="TNCN",
        tax_scope={
            "tax_type": ["PIT"],
            "income_type": ["salary", "business", "capital", "transfer"],
            "taxpayer_type": ["individual", "household"],
            "regime": ["progressive", "flat"],
            "affects": ["calculation", "deduction", "exemption"],
        },
        tags=["TNCN", "thuế thu nhập", "cá nhân", "2026"],
    ),

    # ==============================
    # NGHỊ QUYẾT GIẢM TRỪ GIA CẢNH
    # ==============================
    "110_2025_UBTVQH15": LegalDocument(
        doc_id="nghi_quyet_gtgc_2025",
        doc_type="resolution",
        number="110/2025/UBTVQH15",
        title="Nghị quyết điều chỉnh mức giảm trừ gia cảnh",
        issued_by="Ủy ban Thường vụ Quốc hội",
        issued_date=date(2025, 12, 15),
        effective_from=date(2026, 1, 1),  # NQ110 áp dụng từ 01/01/2026, khác Luật 109 (01/07/2026)
        legal_level=2,
        amends=["luat_thue_tncn_2025"],
        category="TNCN",
        tax_scope={
            "tax_type": ["PIT"],
            "income_type": ["salary"],
            "taxpayer_type": ["individual"],
            "affects": ["deduction"],
        },
        tags=["GTGC", "giảm trừ", "gia cảnh", "11 triệu"],
    ),

    # ==============================
    # LUẬT GTGT 2025
    # ==============================
    "149_2025_QH15": LegalDocument(
        doc_id="luat_gtgt_2025",
        doc_type="law",
        number="149/2025/QH15",
        title="Luật Thuế giá trị gia tăng (sửa đổi)",
        issued_by="Quốc hội",
        issued_date=date(2025, 11, 30),
        effective_from=date(2026, 1, 1),
        legal_level=1,
        category="GTGT",
        tax_scope={
            "tax_type": ["VAT"],
            "taxpayer_type": ["individual", "household", "enterprise"],
            "affects": ["calculation", "exemption"],
        },
        tags=["GTGT", "VAT", "ngưỡng 100 triệu"],
    ),

    # ==============================
    # NGHỊ QUYẾT PHÁT TRIỂN KINH TẾ TƯ NHÂN
    # ==============================
    "198_2025_QH15": LegalDocument(
        doc_id="nghi_quyet_kt_tu_nhan_2025",
        doc_type="resolution",
        number="198/2025/QH15",
        title="Nghị quyết về cơ chế, chính sách đặc biệt phát triển kinh tế tư nhân",
        issued_by="Quốc hội",
        issued_date=date(2025, 12, 20),
        effective_from=date(2026, 1, 1),
        legal_level=2,
        category="Policy",
        tax_scope={
            "tax_type": ["PIT", "Business"],
            "taxpayer_type": ["household", "individual"],
            "affects": ["tax_regime_change"],
        },
        tags=["bỏ thuế khoán", "lệ phí môn bài", "hộ kinh doanh", "tự khai"],
    ),

    # ==============================
    # NGHỊ ĐỊNH 20/2026 - ƯU ĐÃI THUẾ
    # ==============================
    "20_2026_NDCP": LegalDocument(
        doc_id="nghi_dinh_20_2026",
        doc_type="decree",
        number="20/2026/NĐ-CP",
        title="Nghị định hướng dẫn NQ 198 về ưu đãi thuế",
        issued_by="Chính phủ",
        issued_date=date(2026, 1, 15),
        effective_from=date(2026, 1, 1),
        legal_level=3,
        implements=["nghi_quyet_kt_tu_nhan_2025"],
        category="TNCN",
        tax_scope={
            "tax_type": ["PIT"],
            "income_type": ["business"],
            "taxpayer_type": ["household"],
            "affects": ["exemption", "incentive"],
        },
        tags=["ưu đãi", "hộ kinh doanh"],
    ),

    # ==============================
    # NGHỊ ĐỊNH 117/2025 - TMĐT
    # ==============================
    "117_2025_NDCP": LegalDocument(
        doc_id="nghi_dinh_tmdt_2025",
        doc_type="decree",
        number="117/2025/NĐ-CP",
        title="Nghị định về quản lý thuế đối với TMĐT",
        issued_by="Chính phủ",
        issued_date=date(2025, 6, 15),
        effective_from=date(2025, 7, 1),
        legal_level=3,
        category="TMDT",
        tax_scope={
            "tax_type": ["PIT", "VAT"],
            "income_type": ["ecommerce"],
            "taxpayer_type": ["household", "individual"],
            "activity_type": ["ecommerce"],
            "affects": ["filing", "calculation"],
        },
        tags=["TMĐT", "thương mại điện tử", "sàn khai thay"],
    ),

    # ==============================
    # NGHỊ ĐỊNH 373/2025 - QUYẾT TOÁN
    # ==============================
    "373_2025_NDCP": LegalDocument(
        doc_id="nghi_dinh_quyet_toan_2025",
        doc_type="decree",
        number="373/2025/NĐ-CP",
        title="Nghị định sửa đổi về quyết toán thuế TNCN",
        issued_by="Chính phủ",
        issued_date=date(2025, 12, 30),
        effective_from=date(2026, 2, 14),
        legal_level=3,
        amends=["nghi_dinh_126_2020"],
        category="TNCN",
        tax_scope={
            "tax_type": ["PIT"],
            "affects": ["filing", "settlement"],
        },
        tags=["quyết toán", "hồ sơ thuế"],
    ),

    # ==============================
    # NGHỊ ĐỊNH 125/2020 - XỬ PHẠT (bản gốc)
    # ==============================
    "125_2020_NDCP": LegalDocument(
        doc_id="nghi_dinh_xu_phat_goc_2020",
        doc_type="decree",
        number="125/2020/NĐ-CP",
        title="Nghị định quy định xử phạt vi phạm hành chính về thuế, hóa đơn",
        issued_by="Chính phủ",
        issued_date=date(2020, 10, 19),
        effective_from=date(2020, 12, 5),
        legal_level=3,
        category="Penalty",
        tax_scope={
            "tax_type": ["PIT", "VAT", "CIT"],
            "affects": ["penalty", "compliance", "invoice"],
        },
        tags=["xử phạt", "vi phạm", "hóa đơn", "mức phạt", "chế tài", "125/2020"],
    ),

    # ==============================
    # NGHỊ ĐỊNH 310/2025 - XỬ PHẠT
    # ==============================
    "310_2025_NDCP": LegalDocument(
        doc_id="nghi_dinh_xu_phat_2025",
        doc_type="decree",
        number="310/2025/NĐ-CP",
        title="Nghị định về xử phạt vi phạm hành chính thuế",
        issued_by="Chính phủ",
        issued_date=date(2025, 12, 1),
        effective_from=date(2026, 1, 16),
        legal_level=3,
        category="Penalty",
        tax_scope={
            "tax_type": ["PIT", "VAT", "CIT"],
            "affects": ["penalty", "compliance"],
        },
        tags=["xử phạt", "vi phạm", "chế tài"],
    ),

    # ==============================
    # THÔNG TƯ 152/2025 - KẾ TOÁN HKD
    # ==============================
    "152_2025_TTBTC": LegalDocument(
        doc_id="thong_tu_ke_toan_hkd_2025",
        doc_type="circular",
        number="152/2025/TT-BTC",
        title="Thông tư hướng dẫn chế độ kế toán cho hộ kinh doanh",
        issued_by="Bộ Tài chính",
        issued_date=date(2025, 12, 20),
        effective_from=date(2026, 1, 1),
        legal_level=4,
        implements=["nghi_quyet_kt_tu_nhan_2025"],
        category="Accounting",
        tax_scope={
            "tax_type": ["PIT"],
            "taxpayer_type": ["household"],
            "affects": ["bookkeeping", "compliance"],
        },
        tags=["kế toán", "hộ kinh doanh", "sổ sách"],
    ),

    # ==============================
    # NGHỊ ĐỊNH 68/2026 - THUẾ HKD
    # ==============================
    "68_2026_NDCP": LegalDocument(
        doc_id="nghi_dinh_thue_hkd_2026",
        doc_type="decree",
        number="68/2026/NĐ-CP",
        title="Nghị định quy định về chính sách thuế và quản lý thuế đối với hộ kinh doanh",
        issued_by="Chính phủ",
        issued_date=date(2026, 3, 5),
        effective_from=date(2026, 3, 5),
        legal_level=3,
        category="HKD",
        tax_scope={
            "tax_type": ["PIT", "VAT"],
            "taxpayer_type": ["household"],
            "regime": ["flat", "declaration"],
            "affects": ["calculation", "compliance", "registration"],
        },
        tags=["hộ kinh doanh", "thuế khoán", "khai thuế", "68/2026"],
    ),

    # ==============================
    # THÔNG TƯ 18/2026 - THỦ TỤC HKD
    # ==============================
    "18_2026_TTBTC": LegalDocument(
        doc_id="thong_tu_thu_tuc_hkd_2026",
        doc_type="circular",
        number="18/2026/TT-BTC",
        title="Thông tư quy định về hồ sơ, thủ tục quản lý thuế đối với hộ kinh doanh",
        issued_by="Bộ Tài chính",
        issued_date=date(2026, 3, 5),
        effective_from=date(2026, 3, 5),
        legal_level=4,
        implements=["nghi_dinh_thue_hkd_2026"],
        category="HKD",
        tax_scope={
            "tax_type": ["PIT", "VAT"],
            "taxpayer_type": ["household"],
            "affects": ["registration", "compliance", "procedure"],
        },
        tags=["hộ kinh doanh", "hồ sơ", "thủ tục", "đăng ký", "18/2026"],
    ),

    # ==============================
    # THÔNG TƯ 111/2013 - HƯỚNG DẪN LUẬT TNCN
    # ==============================
    "111_2013_TTBTC": LegalDocument(
        doc_id="thong_tu_111_2013",
        doc_type="circular",
        number="111/2013/TT-BTC",
        title="Thông tư hướng dẫn thực hiện Luật Thuế thu nhập cá nhân",
        issued_by="Bộ Tài chính",
        issued_date=date(2013, 8, 15),
        effective_from=date(2013, 10, 1),
        effective_to=date(2026, 6, 30),  # Hết hiệu lực khi Luật 109/2025/QH15 có hiệu lực từ 01/07/2026
        legal_level=4,
        category="TNCN",
        tax_scope={
            "tax_type": ["PIT"],
            "income_type": ["salary", "business", "capital", "transfer"],
            "taxpayer_type": ["individual"],
            "affects": ["calculation", "deduction", "exemption", "withholding", "settlement"],
        },
        tags=["TNCN", "giảm trừ gia cảnh", "khấu trừ thuế", "ủy quyền quyết toán", "111/2013"],
    ),

    # ==============================
    # THÔNG TƯ 92/2015 - SỬA ĐỔI TT111 (GIẢM TRỪ GIA CẢNH, NPT)
    # ==============================
    "92_2015_TTBTC": LegalDocument(
        doc_id="thong_tu_92_2015",
        doc_type="circular",
        number="92/2015/TT-BTC",
        title="Thông tư hướng dẫn thực hiện thuế TNCN đối với cá nhân không cư trú và sửa đổi TT111/2013",
        issued_by="Bộ Tài chính",
        issued_date=date(2015, 6, 15),
        effective_from=date(2015, 8, 1),
        effective_to=date(2026, 6, 30),  # Hết hiệu lực khi Luật 109/2025/QH15 có hiệu lực từ 01/07/2026
        legal_level=4,
        category="TNCN",
        tax_scope={
            "tax_type": ["PIT"],
            "income_type": ["salary", "business", "capital"],
            "taxpayer_type": ["individual"],
            "affects": ["deduction", "exemption", "calculation", "settlement"],
        },
        tags=["TNCN", "giảm trừ gia cảnh", "người phụ thuộc", "NPT hồi tố", "92/2015"],
    ),

    # ==============================
    # NGHỊ ĐỊNH 126/2020 - QUẢN LÝ THUẾ
    # ==============================
    "126_2020_NDCP": LegalDocument(
        doc_id="nghi_dinh_126_2020",
        doc_type="decree",
        number="126/2020/NĐ-CP",
        title="Nghị định quy định chi tiết một số điều của Luật Quản lý thuế",
        issued_by="Chính phủ",
        issued_date=date(2020, 10, 19),
        effective_from=date(2020, 12, 5),
        legal_level=3,
        category="TNCN",
        tax_scope={
            "tax_type": ["PIT", "VAT", "CIT"],
            "taxpayer_type": ["individual", "household", "enterprise"],
            "affects": ["filing", "settlement", "compliance", "withholding"],
        },
        tags=["quản lý thuế", "quyết toán TNCN", "ủy quyền quyết toán", "126/2020"],
    ),

    # ==============================
    # THÔNG TƯ 86/2024 - ĐĂNG KÝ MST
    # ==============================
    "86_2024_TTBTC": LegalDocument(
        doc_id="thong_tu_86_2024",
        doc_type="circular",
        number="86/2024/TT-BTC",
        title="Thông tư hướng dẫn về đăng ký thuế",
        issued_by="Bộ Tài chính",
        issued_date=date(2024, 11, 22),
        effective_from=date(2025, 1, 6),
        legal_level=4,
        category="TNCN",
        tax_scope={
            "tax_type": ["PIT"],
            "taxpayer_type": ["individual"],
            "affects": ["registration", "compliance"],
        },
        tags=["đăng ký thuế", "MST", "người phụ thuộc", "CCCD", "86/2024"],
    ),

    # ==============================
    # CÔNG VĂN 1296/CTNVT - HƯỚNG DẪN QUYẾT TOÁN TNCN
    # ==============================
    "1296_CTNVT": LegalDocument(
        doc_id="cong_van_quyet_toan_tncn_1296",
        doc_type="guidance",
        number="1296/CTNVT",
        title="Công văn hướng dẫn quyết toán thuế thu nhập cá nhân",
        issued_by="Cục Thuế",
        issued_date=date(2026, 3, 15),
        effective_from=date(2026, 1, 1),
        legal_level=5,
        category="TNCN",
        tax_scope={
            "tax_type": ["PIT"],
            "taxpayer_type": ["individual"],
            "affects": ["finalization", "compliance"],
        },
        tags=["quyết toán", "TNCN", "cá nhân", "hoàn thuế"],
    ),

    # ==============================
    # LUẬT QUẢN LÝ THUẾ 2025
    # ==============================
    "108_2025_QH15": LegalDocument(
        doc_id="luat_quan_ly_thue_2025",
        doc_type="law",
        number="108/2025/QH15",
        title="Luật Quản lý thuế",
        issued_by="Quốc hội",
        issued_date=date(2025, 12, 10),
        effective_from=date(2026, 7, 1),   # phần lớn có hiệu lực 01/07/2026
        legal_level=1,
        supersedes=["LQT_38_2019"],
        category="QUANLY",
        tax_scope={
            "tax_type": ["ALL"],
            "taxpayer_type": ["individual", "household", "enterprise"],
            "affects": [
                "registration", "declaration", "inspection",
                "enforcement", "refund", "penalty", "agent",
            ],
        },
        tags=[
            "quản lý thuế", "đăng ký thuế", "kê khai", "thanh tra",
            "kiểm tra", "cưỡng chế", "hoàn thuế", "đại lý thuế",
            "gia hạn", "bất khả kháng", "mã số thuế",
        ],
    ),
}


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def get_document_by_id(doc_id: str) -> Optional[LegalDocument]:
    """Get document by doc_id"""
    for doc in DOCUMENT_REGISTRY.values():
        if doc.doc_id == doc_id:
            return doc
    return None


def get_documents_by_category(category: str) -> List[LegalDocument]:
    """Get all documents in a category"""
    return [doc for doc in DOCUMENT_REGISTRY.values() if doc.category == category]


def get_effective_documents(query_date: date = None) -> List[LegalDocument]:
    """Get all documents effective on a given date (default: today)"""
    if query_date is None:
        query_date = date.today()
    
    return [doc for doc in DOCUMENT_REGISTRY.values() if doc.is_effective_on(query_date)]


def get_document_metadata(filename: str) -> Optional[LegalDocument]:
    """Get document metadata from registry by filename"""
    return DOCUMENT_REGISTRY.get(filename)


# ==========================================
# EXPORTS
# ==========================================

__all__ = [
    # Config
    "config",
    "Config",
    
    # Tax constants
    "tax_constants",
    "TaxConstants",
    
    # Model routing
    "model_router",
    "IntelligentModelRouter",
    "RoutingDecision",
    
    # Types
    "TaxScope",
    "Point",
    "Clause",
    "Article",
    "Appendix",
    "LegalDocument",
    
    # Legal hierarchy
    "LEGAL_AUTHORITY_RANK",
    "LegalHierarchyResolver",
    
    # Document registry
    "DOCUMENT_REGISTRY",
    "get_document_by_id",
    "get_documents_by_category",
    "get_effective_documents",
    "get_document_metadata",
]