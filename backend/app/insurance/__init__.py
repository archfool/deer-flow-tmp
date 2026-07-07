"""保险销售领域模块。"""

from app.insurance.models import CustomerProfile
from app.insurance.service import InsuranceService

__all__ = ["CustomerProfile", "InsuranceService"]
