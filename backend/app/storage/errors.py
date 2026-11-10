"""Безопасная ошибка хранилища для HTTP-границы без SQL и клиентских данных."""


class LeadStorageError(RuntimeError):
    pass
