from setuptools import setup, find_packages


setup(
    name='iosql', 
    version='0.1.2', 
    packages=find_packages(),
    description='Partitioned-Sqlite Library',
    install_requires = ['arrow'],
    scripts=[],
    python_requires = '>=3',
    include_package_data=True,
    author='Liu Shengli',
    url='http://github.com/gseismic/iosql',
    zip_safe=False,
    author_email='liushengli203@163.com'
)
