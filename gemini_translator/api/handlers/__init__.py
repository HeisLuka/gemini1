# -----------------------------------------------------------------------------
# AUTO-GENERATED IMPORTS - DO NOT EDIT THIS SECTION MANUALLY
# Run this file as a script to update imports: python __init__.py
# -----------------------------------------------------------------------------

if __name__ != "__main__":
    """
    IMPORTANT:
    We use lazy imports to avoid importing all handlers at package import time.
    This prevents slow startups / hangs caused by heavy modules (browser/local/hf).
    """

    import importlib

    # Auto-generated: class name -> module (relative to this package)
    _HANDLER_IMPORTS = {
        "BrowserApiHandler": ".browser",
        "DryRunApiHandler": ".dry_run",
        "GeminiApiHandler": ".gemini",
        "HuggingFaceApiHandler": ".huggingface",
        "LocalApiHandler": ".local",
        "OpenRouterApiHandler": ".openrouter",
    }

    __all__ = list(_HANDLER_IMPORTS.keys())

    def __getattr__(name: str):
        """
        Lazy attribute loader:
        allows `handlers.GeminiApiHandler` / `from handlers import GeminiApiHandler`
        without importing every handler module at startup.
        """
        mod_rel = _HANDLER_IMPORTS.get(name)
        if not mod_rel:
            raise AttributeError(name)

        mod = importlib.import_module(mod_rel, __name__)
        obj = getattr(mod, name)

        # Cache in module globals so next access is fast and `hasattr` becomes True.
        globals()[name] = obj
        return obj


# =============================================================================
#  SELF-MAINTENANCE SCRIPT (AUTOMATION LOGIC)
# =============================================================================
if __name__ == "__main__":
    import os
    import ast
    import sys

    # Маркер, разделяющий авто-код и логику скрипта
    SEPARATOR = "# ============================================================================="

    def find_handlers(directory):
        """Сканирует папку и ищет классы, заканчивающиеся на 'ApiHandler'."""
        handlers = []  # (filename_no_ext, class_name)

        print(f"🔍 Сканирование директории: {directory}")

        for filename in sorted(os.listdir(directory)):
            if filename.endswith(".py") and filename != "__init__.py":
                filepath = os.path.join(directory, filename)

                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read())

                    for node in tree.body:
                        # Ищем классы: class XyzApiHandler(...)
                        if isinstance(node, ast.ClassDef) and node.name.endswith("ApiHandler"):
                            if node.name == "BaseApiHandler":
                                continue

                            module_name = filename[:-3]  # убираем .py
                            handlers.append((module_name, node.name))
                            print(f"   ✅ Найден: {node.name} в {filename}")

                except Exception as e:
                    print(f"   ⚠️ Ошибка чтения {filename}: {e}")

        return handlers

    def regenerate_self(handlers):
        """Читает себя, сохраняет нижнюю часть и генерирует новую верхнюю."""
        current_file = os.path.abspath(__file__)

        with open(current_file, "r", encoding="utf-8") as f:
            content = f.read()

        if SEPARATOR not in content:
            print("❌ ОШИБКА: Не найден разделитель секций в файле __init__.py!")
            return

        # Сохраняем скрипт (нижнюю часть)
        script_logic = content[content.find(SEPARATOR):]

        # Генерируем новую верхнюю часть (LAZY IMPORTS)
        lines = []
        lines.append("# -----------------------------------------------------------------------------")
        lines.append("# AUTO-GENERATED IMPORTS - DO NOT EDIT THIS SECTION MANUALLY")
        lines.append(f"# Run this file as a script to update imports: python {os.path.basename(current_file)}")
        lines.append("# -----------------------------------------------------------------------------")
        lines.append("")
        lines.append('if __name__ != "__main__":')
        lines.append('    """')
        lines.append("    IMPORTANT:")
        lines.append("    We use lazy imports to avoid importing all handlers at package import time.")
        lines.append("    This prevents slow startups / hangs caused by heavy modules (browser/local/hf).")
        lines.append('    """')
        lines.append("")
        lines.append("    import importlib")
        lines.append("")
        lines.append("    # Auto-generated: class name -> module (relative to this package)")
        lines.append("    _HANDLER_IMPORTS = {")
        for i, (module, classname) in enumerate(handlers):
            lines.append(f'        "{classname}": ".{module}",')
        lines.append("    }")
        lines.append("")
        lines.append("    __all__ = list(_HANDLER_IMPORTS.keys())")
        lines.append("")
        lines.append("    def __getattr__(name: str):")
        lines.append('        """Lazy attribute loader for handler classes."""')
        lines.append("        mod_rel = _HANDLER_IMPORTS.get(name)")
        lines.append("        if not mod_rel:")
        lines.append("            raise AttributeError(name)")
        lines.append("")
        lines.append("        mod = importlib.import_module(mod_rel, __name__)")
        lines.append("        obj = getattr(mod, name)")
        lines.append("        globals()[name] = obj  # cache")
        lines.append("        return obj")
        lines.append("")
        lines.append("")

        # Собираем и пишем
        new_content = "\n".join(lines) + script_logic

        with open(current_file, "w", encoding="utf-8") as f:
            f.write(new_content)

        print(f"✨ Файл {os.path.basename(current_file)} успешно обновлен!")

    # --- ЗАПУСК ---
    current_dir = os.path.dirname(os.path.abspath(__file__))
    found_handlers = find_handlers(current_dir)
    regenerate_self(found_handlers)
