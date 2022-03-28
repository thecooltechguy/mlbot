import pathlib
from setuptools import setup

HERE = pathlib.Path(__file__).parent

README = (HERE / "README.md").read_text()

if __name__ == "__main__":
    setup(
        name='mlbot-cloud',
        version='1.0.0',
        description=" A fast & easy way to train ML models in your cloud, directly from your laptop.",
        long_description=README,
        long_description_content_type="text/markdown",
        url="https://github.com/thecooltechguy/mlbot",
        author="Subhash Ramesh",
        author_email="suramesh@ucsd.edu",
        py_modules=['mlbot'],
        install_requires=[
            "boto3==1.21.18",
            "click==8.0.4",
            "PyYAML==6.0",
            "requests==2.27.1",
            "requests-oauthlib==1.3.1",
            "yaspin==2.1.0"
        ],
        include_package_data=True,
        license="MIT",
        classifiers=[
            "License :: OSI Approved :: MIT License",
            "Programming Language :: Python :: 3",
        ],
        entry_points={
            'console_scripts': [
                'mlbot = mlbot:cli',
            ],
        },
    )