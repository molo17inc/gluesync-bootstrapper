"""
py2app setup configuration for Gluesync Automator macOS app.
"""
from setuptools import setup

APP = ['run_automator.py']
DATA_FILES = []
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'automator_app/static/gluesync-bootstrapper.icns',
    'plist': {
        'CFBundleName': 'Gluesync Automator',
        'CFBundleDisplayName': 'Gluesync Automator',
        'CFBundleIdentifier': 'com.molo17.gluesync-automator',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
    },
    'packages': ['automator_app', 'create_user_defined_functions', 'utils'],
    'includes': ['AppKit', 'WebKit', 'Foundation'],
    'excludes': ['tkinter', 'matplotlib', 'numpy', 'scipy', 'PIL'],
}

setup(
    name='Gluesync Automator',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
