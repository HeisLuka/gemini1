class BaseTaskProcessor:
    """
    Базовый класс для всех обработчиков задач.
    Определяет интерфейс и предоставляет доступ к воркеру.
    """
    def __init__(self, worker):
        self.worker = worker

    async def execute(self, task_info, use_stream=True):
        """
        Основной метод, который должен быть переопределен в дочерних классах.
        """
        raise NotImplementedError("Метод execute должен быть реализован в подклассе.")