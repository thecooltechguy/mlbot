from setuptools import setup

if __name__ == "__main__":
    setup(
        name='mlbot',
        version='0.1.0',
        py_modules=['mlbot'],
        install_requires=[
            "boto3==1.21.18",
            "click==8.0.4",
            "PyYAML==6.0",
            "requests==2.27.1",
            "requests-oauthlib==1.3.1",
            "yaspin==2.1.0"
        ],
        entry_points={
            'console_scripts': [
                'mlbot = mlbot:cli',
            ],
        },
    )