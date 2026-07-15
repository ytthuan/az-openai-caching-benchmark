class BenchmarkError(RuntimeError):
    pass


class ConfigurationError(BenchmarkError):
    pass


class ValidationError(BenchmarkError):
    pass


class PricingError(BenchmarkError):
    pass


class BudgetExhausted(BenchmarkError):
    pass
