# build_master.py (v15.0 - "The Universal Collector")
# Улучшение: ADDITIONAL_DATA теперь универсальна.
# Можно добавлять и папки ('config'), и отдельные файлы ('README.md', 'data/dict.txt').
# Скрипт сам определит тип и сгенерирует корректные команды для PyInstaller и xcopy/copy.
#
# ДОРАБОТАНО ПО ДОГОВОРЁННОСТЯМ:
# 1) build.bat: авто-детект venv/.venv
# 2) build.bat: установка зависимостей и PyInstaller всегда через "%PYTHON_EXE%" -m pip / -m PyInstaller
# 3) Исправлена логика возврата в меню (install_deps -> goto menu)
# 4) Убраны хрупкие replace('pyinstaller', ...) и кавычки внутри переменных команд

import os
import sys
import ast
import importlib.util
from pathlib import Path
import re

# --- ОБЩАЯ КОНФИГУРАЦИЯ ---
PROJECT_ROOT = Path(__file__).parent.resolve()
MAIN_PY_FILE = "main.py"
APP_ICON_FILE = "gemini_translator\\GT.ico"
OUTPUT_BAT_FILE = "build.bat"
OUTPUT_REQUIREMENTS_FILE = "requirements.txt"

# <-- НОВАЯ УНИВЕРСАЛЬНАЯ ПЕРЕМЕННАЯ
# Указывайте здесь папки ИЛИ отдельные файлы, которые должны
# попасть в итоговую сборку с сохранением путей.
ADDITIONAL_DATA = [
    'config',       # Папка целиком
    'README.md',    # Файл в корне
]

EXCLUDE_DIRS = {'venv', '.venv', 'env', '.git', '__pycache__', 'dist', 'build'}
PROJECT_MODULES = {'gemini_translator'}
DEV_MODULES = {'pyinstaller', 'pyinstaller-hooks-contrib'}
DATA_FILE_EXTENSIONS = {'.txt', '.json', '.ico', '.css', '.html', '.js'}
HIDDEN_IMPORTS_BLOCK = [
    'PyQt6.sip', 
    'gemini_translator.api.handlers.gemini'
]

# --- КОНФИГУРАЦИЯ ЗАВИСИМОСТЕЙ ---
IMPORT_TO_PACKAGE_MAP = {
    'socks': 'PySocks',
    'opencc': 'opencc-python-reimplemented',
    'Levenshtein': 'python-Levenshtein',
    'jwt': 'pyjwt',
    'bs4': 'beautifulsoup4',
    'pymorphy2': 'pymorphy3',
    'recognizers_text': 'recognizers-text',
    'recognizers_number': 'recognizers-text-number',
}

ESSENTIAL_PACKAGES = {}
FORCED_VERSIONS = {
    'pydantic': '>=2.0.0',
    'setuptools': '<81',
}
CONFLICTING_PACKAGES_TO_REMOVE = {"os_patch", "pyinstaller_hooks_contrib"}


def find_project_imports():
    print("--- Этап 1: Сканирование файлов проекта для поиска импортов ---")
    all_imports = set()
    
    # Стандартное сканирование всех .py файлов
    for file_path in PROJECT_ROOT.rglob("*.py"):
        if any(part in file_path.parts for part in EXCLUDE_DIRS):
            continue
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            tree = ast.parse(content, filename=str(file_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        all_imports.add(alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level > 0:
                        continue
                    if node.module:
                        all_imports.add(node.module.split('.')[0])
        except Exception as e:
            print(f"  [Предупреждение] Не удалось проанализировать {file_path}: {e}")

    # --- НОВАЯ ЛОГИКА: Автоматический поиск динамических хендлеров ---
    # Мы принудительно ищем все файлы в папке handlers, чтобы PyInstaller их не пропустил
    handlers_dir = PROJECT_ROOT / "gemini_translator" / "api" / "handlers"
    
    if handlers_dir.exists():
        print(f"  -> Поиск динамических модулей в {handlers_dir.relative_to(PROJECT_ROOT)}...")
        for h_file in handlers_dir.glob("*.py"):
            # Пропускаем __init__.py и временные файлы
            if h_file.name == "__init__.py" or h_file.stem.startswith(('_', '.')):
                continue
            
            # Формируем полный путь импорта для PyInstaller
            # Это превратит файл gemini.py в gemini_translator.api.handlers.gemini
            module_path = f"gemini_translator.api.handlers.{h_file.stem}"
            
            if module_path not in HIDDEN_IMPORTS_BLOCK:
                HIDDEN_IMPORTS_BLOCK.append(module_path)
                print(f"     [+] Добавлен скрытый импорт: {module_path}")

    print(f"[OK] Найдено {len(all_imports)} уникальных модулей и {len(HIDDEN_IMPORTS_BLOCK)} скрытых импортов.")
    return all_imports

def filter_third_party_imports(imports_):
    print("\n--- Этап 2: Фильтрация модулей (улучшенная логика) ---")
    third_party_imports = set()

    # 1. Получаем список стандартных библиотек. Это наш "черный список".
    try:
        # Для Python 3.10+
        standard_libs = set(sys.stdlib_module_names)
        print(f"  -> Используется полный список стандартных библиотек Python {sys.version.split()[0]}.")
    except AttributeError:
        # Для более старых версий Python (fallback)
        standard_libs = set(sys.builtin_module_names)
        print(f"  -> [WARN] Используется базовый список встроенных модулей. Точность может быть ниже.")

    # 2. Итерируем по всем найденным импортам
    for module_name in sorted(list(imports_)):
        # 3. Применяем простое правило исключения
        if module_name in PROJECT_MODULES or module_name in standard_libs:
            continue

        # 4. ВСЁ ОСТАЛЬНОЕ - считаем сторонней зависимостью!
        third_party_imports.add(module_name)

    print("[OK] Идентифицированы сторонние зависимости по принципу исключения.")
    return third_party_imports


def apply_package_mapping(dependencies):
    print("\n--- Этап 3: Применение карты 'импорт -> пакет' ---")
    remapped_deps = set()
    for dep in dependencies:
        if dep in IMPORT_TO_PACKAGE_MAP:
            package_name = IMPORT_TO_PACKAGE_MAP[dep]
            remapped_deps.add(package_name)
            print(f"  -> Переназначен импорт '{dep}' на пакет '{package_name}'.")
        else:
            remapped_deps.add(dep)
    print("[OK] Переназначение завершено.")
    return remapped_deps


def update_requirements_file(dependencies):
    print(f"\n--- Этап 4: Обновление '{OUTPUT_REQUIREMENTS_FILE}' ---")
    filtered_deps = dependencies - DEV_MODULES - CONFLICTING_PACKAGES_TO_REMOVE
    final_dependencies = set()

    for dep in filtered_deps:
        dep_lower = dep.lower()
        if dep_lower in FORCED_VERSIONS:
            final_dependencies.add(f"{dep}{FORCED_VERSIONS[dep_lower]}")
        else:
            final_dependencies.add(dep)

    sorted_deps = sorted(list(final_dependencies), key=str.lower)

    try:
        with open(OUTPUT_REQUIREMENTS_FILE, 'w', encoding='utf-8') as f:
            f.write("# Сгенерировано автоматически скриптом build_master.py\n")
            f.write("\n".join(sorted_deps) + "\n")
        print(f"[OK] Файл '{OUTPUT_REQUIREMENTS_FILE}' успешно обновлен.")
        # возвращаем “чистые” имена для analyze_dependencies_for_pyinstaller_flags
        return [re.split(r'[>=<]', dep)[0] for dep in sorted_deps]
    except Exception as e:
        print(f"[ОШИБКА] Не удалось записать в '{OUTPUT_REQUIREMENTS_FILE}': {e}")
        return []


def analyze_dependencies_for_pyinstaller_flags(dependencies):
    print(f"\n--- Этап 5: Анализ пакетов для PyInstaller ---")
    collect_data_flags = set()

    for package_name in dependencies:
        try:
            spec = importlib.util.find_spec(package_name)
            if not spec or not spec.origin:
                continue

            package_dir = Path(spec.origin).parent
            has_data_files = any(
                fp.is_file() and fp.suffix.lower() in DATA_FILE_EXTENSIONS
                for fp in package_dir.rglob('*')
                if '.dist-info' not in fp.parts and '.egg-info' not in fp.parts
            )
            if has_data_files:
                collect_data_flags.add(package_name)
        except Exception:
            pass

    if collect_data_flags:
        print(f"  -> Обнаружены и добавлены флаги сбора для: {', '.join(sorted(collect_data_flags))}")
    return collect_data_flags


def _join_cmd_for_bat(args: list[str]) -> str:
    """
    Склеиваем аргументы в красивый многострочный bat-командный блок через ^
    """
    return " ^\n".join(args)


def generate_pure_bat_script(dependencies, collect_data_flags):
    print(f"\n--- Этап 6: Генерация универсального лаунчера '{OUTPUT_BAT_FILE}' ---")

    hooks_block = []
    try:
        import pyinstaller_hooks_contrib
        hooks_path = pyinstaller_hooks_contrib.get_hook_dirs()[0]
        hooks_block.append(f'--additional-hooks-dir="{hooks_path}"')
        print("[OK] Найдены хуки сообщества (pyinstaller-hooks-contrib).")
    except (ImportError, IndexError):
        print("[ПРЕДУПРЕЖДЕНИЕ] pyinstaller-hooks-contrib не найден (в bat он всё равно будет ставиться при сборке).")

    data_block = [f'--collect-data="{data}"' for data in sorted(list(collect_data_flags))]
    hidden_imports_args = [f'--hidden-import="{imp}"' for imp in HIDDEN_IMPORTS_BLOCK]

    # ВАЖНО: здесь БЕЗ "pyinstaller". Его будет вызывать бат через python -m PyInstaller
    base_pyinstaller_args = [
        MAIN_PY_FILE,
        "--windowed",
        '--name="%AppName%"',
        "--clean",
        f'--icon="{APP_ICON_FILE}"',
        "--noconfirm",
    ]
    base_pyinstaller_args.extend(hooks_block)
    base_pyinstaller_args.extend(data_block)
    base_pyinstaller_args.extend(hidden_imports_args)

    # --- НОВАЯ ЛОГИКА ДЛЯ DATA ---
    add_data_args = []
    copy_commands_hybrid = []
    copy_commands_advanced = []

    print("  -> Анализ ADDITIONAL_DATA для включения в сборку:")
    for item in ADDITIONAL_DATA:
        path = Path(item)
        if not path.exists():
            print(f"     [WARN] Элемент не найден и будет пропущен: {item}")
            continue

        # 1) --add-data (упаковываем внутрь)
        dest_arg = '.'
        if path.is_dir():
            print(f"     - Папка: {item}")
            dest_arg = item
        elif path.is_file():
            print(f"     - Файл:  {item}")
            parent_dir = path.parent
            dest_arg = str(parent_dir) if str(parent_dir) != '.' else '.'

        add_data_args.append(f'--add-data "{item};{dest_arg}"')

        # 2) Команды копирования для гибридного/продвинутого режима (если тебе нужно рядом с exe)
        win_path = str(path).replace('/', '\\')
        if path.is_dir():
            copy_commands_hybrid.append(
                f'    xcopy "{win_path}" "dist\\{win_path}\\" /E /I /Y /Q > nul'
            )
            copy_commands_advanced.append(
                f'    xcopy "{win_path}" "dist\\%AppName%\\{win_path}\\" /E /I /Y /Q > nul'
            )
        elif path.is_file():
            win_parent = str(path.parent).replace('/', '\\')
            if str(path.parent) == '.':
                copy_commands_hybrid.append(
                    f'    copy /Y "{win_path}" "dist\\{win_path}" > nul'
                )
                copy_commands_advanced.append(
                    f'    copy /Y "{win_path}" "dist\\%AppName%\\{win_path}" > nul'
                )
            else:
                copy_commands_hybrid.append(
                    f'    if not exist "dist\\{win_parent}" mkdir "dist\\{win_parent}"'
                )
                copy_commands_hybrid.append(
                    f'    copy /Y "{win_path}" "dist\\{win_path}" > nul'
                )
                copy_commands_advanced.append(
                    f'    if not exist "dist\\%AppName%\\{win_parent}" mkdir "dist\\%AppName%\\{win_parent}"'
                )
                copy_commands_advanced.append(
                    f'    copy /Y "{win_path}" "dist\\%AppName%\\{win_path}" > nul'
                )

    # Команды PyInstaller (АРГУМЕНТЫ)
    full_portable_args = list(base_pyinstaller_args) + ["--onefile"] + add_data_args
    hybrid_args = list(base_pyinstaller_args) + ["--onefile"] + add_data_args
    advanced_args = list(base_pyinstaller_args) + add_data_args

    pyinstaller_args_full_portable = _join_cmd_for_bat(full_portable_args)
    pyinstaller_args_hybrid = _join_cmd_for_bat(hybrid_args)
    pyinstaller_args_advanced = _join_cmd_for_bat(advanced_args)

    hybrid_copy_block = "\n".join(copy_commands_hybrid)
    advanced_copy_block = "\n".join(copy_commands_advanced)

    bat_content = f"""@echo off
setlocal
cls

:: ============================================================================
:: Универсальный лаунчер GeminiTranslator
:: Сгенерировано: build_master.py (v15.0 - "The Universal Collector")
:: ============================================================================

:: --- Этап 0: Выбор Python (venv/.venv приоритетнее системного) ---
set "PYTHON_EXE=python"
if exist "%~dp0venv\\Scripts\\python.exe" (
    set "PYTHON_EXE=%~dp0venv\\Scripts\\python.exe"
    echo [+] Найдено виртуальное окружение: venv
) else if exist "%~dp0.venv\\Scripts\\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\\Scripts\\python.exe"
    echo [+] Найдено виртуальное окружение: .venv
) else (
    echo [!] venv/.venv не найден. Будет использован системный Python.
)
echo [+] Python: "%PYTHON_EXE%"
echo.

:: --- Этап 1: Проверка и запрос прав администратора (если нужно) ---
>nul 2>&1 net session
if '%errorlevel%' NEQ '0' (
    echo.
    echo [+] Запрос прав администратора...
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\\getadmin.vbs"
    echo UAC.ShellExecute "%~f0", "", "", "runas", 1 >> "%temp%\\getadmin.vbs"
    cscript "%temp%\\getadmin.vbs" & exit /B
)
if exist "%temp%\\getadmin.vbs" ( del "%temp%\\getadmin.vbs" )

:: --- Этап 2: Настройка рабочего окружения ---
cd /d "%~dp0"
echo [+] Рабочая директория: %cd%

for %%I in ("%cd%") do set "AppName=%%~nxI"
echo [+] Имя приложения будет: %AppName%
echo.

:: --- Главное меню ---
:menu
cls
echo ======================================================
echo   Универсальный лаунчер для: %AppName%
echo ======================================================
echo.
echo   1. Установить / Обновить зависимости программы
echo.
echo   2. Собрать приложение
echo.
echo   3. Выход
echo.
echo ======================================================
set /p choice="Выберите действие (1, 2 или 3): "

if not defined choice ( goto menu )
if "%choice%"=="1" ( goto install_deps )
if "%choice%"=="2" ( goto build_menu )
if "%choice%"=="3" ( goto :eof )

echo Неверный выбор. Пожалуйста, введите 1, 2 или 3.
pause
goto menu


:: --- Меню сборки ---
:build_menu
cls
echo ======================================================
echo   Выберите тип сборки
echo ======================================================
echo.
echo   1. ПОЛНОСТЬЮ ПОРТАТИВНАЯ (один .exe файл)
echo      - Создает один .exe файл. Все встроено внутрь.
echo      - Легко распространять, но настройки менять нельзя.
echo      - Рекомендуется для большинства пользователей.
echo.
echo   2. ГИБРИДНАЯ (один .exe + папки с данными)
echo      - Создает один .exe и рядом с ним папки с данными.
echo      - Сочетает портативность и возможность менять конфиги.
echo      - Рекомендуется для опытных пользователей.
echo.
echo   3. ПРОДВИНУТАЯ (папка с файлами)
echo      - Создает папку с .exe и всеми зависимостями.
echo      - Позволяет вручную редактировать конфиги и данные.
echo      - Для разработчиков и отладки.
echo.
echo   4. Назад в главное меню
echo.
echo ======================================================
set /p build_choice="Выберите действие (1, 2, 3 или 4): "

if not defined build_choice ( goto build_menu )
if "%build_choice%"=="1" ( goto build_full_portable )
if "%build_choice%"=="2" ( goto build_hybrid )
if "%build_choice%"=="3" ( goto build_advanced )
if "%build_choice%"=="4" ( goto menu )

echo Неверный выбор.
pause
goto build_menu


:: --- Блок установки зависимостей ---
:install_deps
cls
echo --- Установка / обновление зависимостей программы ---
echo.
echo [+] Запуск установки из файла '{OUTPUT_REQUIREMENTS_FILE}'...
"%PYTHON_EXE%" -m pip install --upgrade pip
"%PYTHON_EXE%" -m pip install --upgrade -r "{OUTPUT_REQUIREMENTS_FILE}"
if %ERRORLEVEL% NEQ 0 (
    echo [!!!] Ошибка при установке. Проверьте подключение к интернету.
) else (
    echo [OK] Все зависимости успешно установлены/обновлены.
)
echo.
pause
goto menu


:: --- Блок сборки: ПОЛНОСТЬЮ ПОРТАТИВНАЯ ---
:build_full_portable
call :build_app_base "ПОЛНОСТЬЮ ПОРТАТИВНАЯ"
"%PYTHON_EXE%" -m PyInstaller ^
{pyinstaller_args_full_portable}
call :build_app_end
goto :eof


:: --- Блок сборки: ГИБРИДНАЯ ---
:build_hybrid
call :build_app_base "ГИБРИДНАЯ"
"%PYTHON_EXE%" -m PyInstaller ^
{pyinstaller_args_hybrid}
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [+] Этап 3 из 3: Копирование внешних данных...
{hybrid_copy_block}
    echo [OK] Данные скопированы.
)
call :build_app_end
goto :eof


:: --- Блок сборки: ПРОДВИНУТАЯ ---
:build_advanced
call :build_app_base "ПРОДВИНУТАЯ"
"%PYTHON_EXE%" -m PyInstaller ^
{pyinstaller_args_advanced}
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [+] Этап 3 из 3: Копирование внешних данных...
{advanced_copy_block}
    echo [OK] Данные скопированы.
)
call :build_app_end
goto :eof


:: --- ОБЩАЯ ЛОГИКА СБОРКИ ---
:build_app_base
cls
echo --- Полный цикл сборки (%~1 версия) ---
echo.
echo [+] Этап 1 из 3: Установка/обновление всех зависимостей и инструментов...
"%PYTHON_EXE%" -m pip install --upgrade pip
"%PYTHON_EXE%" -m pip install --upgrade -r "{OUTPUT_REQUIREMENTS_FILE}" pyinstaller pyinstaller-hooks-contrib
if %ERRORLEVEL% NEQ 0 (
    echo [!!!] Ошибка при установке зависимостей. Проверьте подключение к интернету.
    pause
    goto menu
)
echo [+] Инструменты для сборки готовы.
echo.
echo [+] Этап 2 из 3: Запуск PyInstaller для сборки "%AppName%"...
echo.
goto :eof


:build_app_end
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!!!] СБОРКА ЗАВЕРШИЛАСЬ С ОШИБКОЙ!
    echo     Просмотрите сообщения выше, чтобы найти причину.
) else (
    echo.
    echo [OK] СБОРКА УСПЕШНО ЗАВЕРШЕНА!
    echo     Готовое приложение находится в папке 'dist'.
)
echo.
echo [+] Процесс завершен.
pause
goto :eof
"""

    try:
        with open(OUTPUT_BAT_FILE, 'w', encoding='cp866') as f:
            f.write(bat_content)
        print(f"[OK] Универсальный лаунчер '{OUTPUT_BAT_FILE}' успешно сгенерирован.")
    except Exception as e:
        print(f"[ОШИБКА] Не удалось записать файл '{OUTPUT_BAT_FILE}': {e}")


if __name__ == "__main__":
    all_imports = find_project_imports()
    third_party_deps = filter_third_party_imports(all_imports)
    remapped_deps = apply_package_mapping(third_party_deps)

    print(f"\n--- Применение правил из конфигурации ---")
    remapped_deps.update(ESSENTIAL_PACKAGES)
    remapped_deps.update(FORCED_VERSIONS.keys())
    print(f"  -> Добавлены обязательные пакеты.")

    print(f"\nИтоговый список зависимостей: {', '.join(sorted(list(remapped_deps)))}")
    final_deps_names = update_requirements_file(remapped_deps)
    if final_deps_names:
        data_flags = analyze_dependencies_for_pyinstaller_flags(final_deps_names)
        generate_pure_bat_script(final_deps_names, data_flags)
        print("\n" + "=" * 60 + "\n[ГОТОВО] УНИВЕРСАЛЬНЫЙ ЛАУНЧЕР ГОТОВ!\n" + "=" * 60)
