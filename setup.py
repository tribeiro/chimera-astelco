from distutils.core import setup

setup(
    name='chimera_astelco',
    version='0.0.1',
    packages=['chimera_astelco', 'chimera_astelco.instruments'],
    scripts=['scripts/chimera-astelcopm'],
    url='http://github.com/astroufsc/chimera_template',
    license='GPL v2',
    author='Tiago Ribeiro',
    author_email='tribeiro@ufs.br',
    description='Chimera pluging for astelco system.'
)
