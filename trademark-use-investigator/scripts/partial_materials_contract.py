#!/usr/bin/env python3
"""Shared immutable names and machine-only state for customer-requested partial materials."""

PARTIAL_PDF_NAME = "partial-investigation-materials.pdf"
PARTIAL_MANIFEST_NAME = "partial-investigation-materials.manifest.json"
PARTIAL_PUBLISHED_RECORD_NAME = "cherrystudio-partial-published-materials.json"
PARTIAL_RECEIPT_NAME = "partial-investigation-materials.receipt.json"
PARTIAL_REQUEST_RECORD_NAME = "partial-investigation-materials.request.json"
PARTIAL_MANIFEST_RECORD_TYPE = "partial_investigation_materials_manifest"
PARTIAL_PUBLISHED_RECORD_TYPE = "cherrystudio_partial_published_materials"
PARTIAL_RECEIPT_RECORD_TYPE = "partial_investigation_materials_receipt"
PARTIAL_REQUEST_RECORD_TYPE = "partial_investigation_materials_request"
PARTIAL_STATUS = "incomplete_partial_materials"

# These strings were previously rendered as conspicuous PDF disclosures.  Keep
# them only as a rejection list so an old or regressed renderer cannot publish
# the unwanted banner again.  Incomplete/partial state belongs in the distinct
# filename and JSON contract, not in the visible PDF pages.
PARTIAL_PDF_PROHIBITED_DISCLOSURES = (
    "调查未完成｜部分调查材料｜不得作为完整覆盖或最终结论",
    "调查未完成｜部分调查材料",
    "调查未完成与任务覆盖缺口",
    "重要：调查仍未完成",
    "材料性质：客户明确要求生成的未完成调查部分材料",
    "不得据此声称检索已全面完成",
)
