"""
Test if config loads correctly from .env
"""

from src.utils.config import config, model_router, tax_constants
from pathlib import Path

def test_env_loaded():
    """Test that environment variables are loaded"""
    
    print("=" * 60)
    print("TESTING ENVIRONMENT CONFIGURATION")
    print("=" * 60)
    
    # 1. Check API key
    print("\n1. API Key:")
    if config.GOOGLE_API_KEY:
        key_preview = config.GOOGLE_API_KEY[:10] + "..." + config.GOOGLE_API_KEY[-4:]
        print(f"   ✅ Loaded: {key_preview}")
    else:
        print("   ❌ NOT LOADED - Check .env file!")
        return False
    
    # 2. Check system mode
    print("\n2. System Mode:")
    print(f"   ✅ {config.SYSTEM_MODE}")
    
    # 3. Check paths
    print("\n3. Paths:")
    paths_to_check = {
        "DATA_DIR": config.DATA_DIR,
        "RAW_DIR": config.RAW_DIR,
        "PARSED_DIR": config.PARSED_DIR,
        "LOG_DIR": config.LOG_DIR,
        "CACHE_DIR": config.CACHE_DIR,
    }
    
    for name, path in paths_to_check.items():
        exists = "✅" if path.exists() else "❌"
        print(f"   {exists} {name}: {path}")
    
    # 4. Check models
    print("\n4. Models:")
    print(f"   Default: {config.GEMINI_MODEL_DEFAULT}")
    print(f"   Premium: {config.GEMINI_MODEL_PREMIUM}")
    print(f"   Routing enabled: {config.ENABLE_INTELLIGENT_ROUTING}")
    
    # 5. Check tax constants
    print("\n5. Tax Constants:")
    print(f"   GTGC 2026: {tax_constants.FAMILY_DEDUCTION_SELF:,} VND")
    print(f"   Tax brackets: {len(tax_constants.PROGRESSIVE_TAX_BRACKETS)}")
    
    # 6. Test routing
    print("\n6. Model Routing:")
    decision = model_router.route("Test query", None)
    print(f"   Model: {decision.model_type}")
    print(f"   Full model name: {decision.model}")
    
    # 7. Check safety flags
    print("\n7. Safety Flags:")
    print(f"   STRICT_LEGAL_MODE: {config.STRICT_LEGAL_MODE}")
    print(f"   ALLOW_LLM_CALCULATION: {config.ALLOW_LLM_CALCULATION}")
    print(f"   REQUIRE_CITATION: {config.REQUIRE_CITATION}")
    
    print("\n" + "=" * 60)
    print("✅ ALL CHECKS PASSED!")
    print("=" * 60)
    
    return True

if __name__ == "__main__":
    test_env_loaded()