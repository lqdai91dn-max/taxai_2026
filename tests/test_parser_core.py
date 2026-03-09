# test nhanh trong terminal
from src.parsing.state_machine.parser_core import StateMachineParser

# Test Nghị định (document_type khác "Luật")
parser = StateMachineParser(
    document_id="117_2025_NDCP",
    document_number="117/2025/NĐ-CP",
    document_type="Nghị định"  # ← phải ra đúng trong output
)

# Kiểm tra breadcrumb gốc
print(parser.document_type)  # → "Nghị định"