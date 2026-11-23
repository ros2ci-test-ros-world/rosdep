# Copyright (c) 2026, The ros2ci authors
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the Willow Garage, Inc. nor the names of its
#       contributors may be used to endorse or promote products derived from
#       this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import os

import pytest

from rosdep2.core import InvalidData
from rosdep2.platforms import conda
from rosdep2.platforms.conda import CondaInstaller, Constraints

CONSTRAINTS = """\
channels: [our-channel, conda-forge]
base:
  python: 3.12.*
pins:
  cmake: 3.31.*
  pytest: 8.*
  flake8: ==7.1.1
  numpy: 2.2
"""


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A constraints file, a manifest path, and a fresh installer."""
    constraints = tmp_path / 'constraints.yaml'
    constraints.write_text(CONSTRAINTS)
    manifest = tmp_path / 'ws' / 'pixi.toml'
    monkeypatch.setenv(conda.CONSTRAINTS_ENV, str(constraints))
    monkeypatch.setenv(conda.MANIFEST_ENV, str(manifest))
    monkeypatch.setenv('ROS_DISTRO', 'rolling')
    conda._constraints_cache.clear()
    yield constraints, manifest, CondaInstaller()
    conda._constraints_cache.clear()


def dependencies(manifest):
    return conda.read_manifest(str(manifest))


def test_parses_constraints():
    c = Constraints.parse(CONSTRAINTS)
    assert c.channels == ['our-channel', 'conda-forge']
    assert c.base == {'python': '3.12.*'}
    assert c.pins['cmake'] == '3.31.*'
    # An unquoted version that YAML reads as a number is still a version.
    assert c.pins['numpy'] == '2.2'


@pytest.mark.parametrize('text', [
    '',
    '- a list',
    'channels: conda-forge',
    'channels: []',
    'pins: {cmake: 3.*}',
    'channels: [conda-forge]\nsurprise: 1',
    'channels: [conda-forge]\npins: [cmake]',
    'channels: [conda-forge]\npins: {"Bad Name": 1.0}',
    'channels: [conda-forge]\npins: {cmake: "1.0\\"\\nevil = \\"x"}',
    'channels: ["has space"]',
    'channels: ["quote\\"d"]',
    'channels: [conda-forge]\npins: {cmake: [1, 2]}',
    'not: [valid',
])
def test_rejects_bad_constraints(text):
    with pytest.raises(InvalidData):
        Constraints.parse(text)


def test_a_key_resolves_only_if_every_package_is_pinned(setup):
    _, _, installer = setup
    assert installer.resolve(['cmake']) == ['cmake']
    assert installer.resolve({'packages': ['cmake', 'pytest']}) == ['cmake', 'pytest']
    assert installer.resolve('cmake pytest') == ['cmake', 'pytest']
    with pytest.raises(InvalidData) as e:
        installer.resolve(['cmake', 'not-pinned'])
    assert 'not-pinned' in str(e.value)
    assert 'cmake,' not in str(e.value)
    with pytest.raises(InvalidData):
        installer.resolve(['Not A Name'])
    with pytest.raises(InvalidData):
        installer.resolve(['evil"\nname'])


def test_ros_packages_need_no_pin(setup):
    _, _, installer = setup
    assert installer.resolve(['ros-rolling-rclcpp']) == ['ros-rolling-rclcpp']
    # Only for the distribution being resolved.
    with pytest.raises(InvalidData):
        installer.resolve(['ros-humble-rclcpp'])


def test_install_writes_a_manifest_and_one_command(setup):
    _, manifest, installer = setup
    commands = installer.get_install_command(['pytest', 'cmake'], interactive=False)
    assert commands == [['pixi', 'install', '--manifest-path', str(manifest)]]
    text = manifest.read_text()
    assert text.splitlines()[0] == conda.MANIFEST_MARKER
    assert 'channels = ["our-channel", "conda-forge"]' in text
    assert 'platforms = ["%s"]' % conda.conda_platform() in text
    # The base packages are always there, and everything is pinned.
    assert dependencies(manifest) == {'python': '3.12.*', 'cmake': '3.31.*', 'pytest': '8.*'}


def test_nothing_to_do_when_everything_is_there(setup):
    _, manifest, installer = setup
    installer.get_install_command(['pytest', 'cmake'])
    before = manifest.read_text()
    assert installer.get_packages_to_install(['pytest', 'cmake']) == []
    assert installer.is_installed('cmake')
    assert not installer.is_installed('flake8')
    assert installer.get_install_command(['pytest', 'cmake']) == []
    assert manifest.read_text() == before


def test_a_rerun_adds_and_never_removes(setup):
    _, manifest, installer = setup
    installer.get_install_command(['pytest', 'cmake'])
    # The package.xml changed: pytest is no longer needed, flake8 is.
    assert installer.get_packages_to_install(['cmake', 'flake8']) == ['flake8']
    installer.get_install_command(['cmake', 'flake8'])
    assert dependencies(manifest) == {
        'python': '3.12.*', 'cmake': '3.31.*', 'pytest': '8.*', 'flake8': '==7.1.1'}


def test_reinstall_writes_the_manifest_afresh(setup):
    _, manifest, installer = setup
    installer.get_install_command(['pytest', 'cmake'])
    assert installer.get_packages_to_install(['cmake', 'flake8'], reinstall=True) == ['cmake', 'flake8']
    commands = installer.get_install_command(['cmake', 'flake8'], reinstall=True)
    assert len(commands) == 1
    assert dependencies(manifest) == {'python': '3.12.*', 'cmake': '3.31.*', 'flake8': '==7.1.1'}


def test_a_changed_pin_moves_the_package(setup):
    constraints, manifest, installer = setup
    installer.get_install_command(['pytest', 'cmake'])
    constraints.write_text(CONSTRAINTS.replace('cmake: 3.31.*', 'cmake: 4.0.*'))
    conda._constraints_cache.clear()
    # cmake counts as not installed, so that rosdep acts.
    assert installer.get_packages_to_install(['pytest', 'cmake']) == ['cmake']
    installer.get_install_command(['pytest', 'cmake'])
    assert dependencies(manifest)['cmake'] == '4.0.*'
    assert dependencies(manifest)['pytest'] == '8.*'


def test_a_package_that_lost_its_pin_keeps_its_version(setup):
    constraints, manifest, installer = setup
    installer.get_install_command(['pytest', 'cmake'])
    constraints.write_text(CONSTRAINTS.replace('  pytest: 8.*\n', ''))
    conda._constraints_cache.clear()
    installer.get_install_command(['cmake', 'flake8'])
    assert dependencies(manifest)['pytest'] == '8.*'
    # But it can no longer be asked for.
    with pytest.raises(InvalidData):
        installer.resolve(['pytest'])
    with pytest.raises(InvalidData):
        installer.get_install_command(['pytest', 'numpy'])


def test_a_manifest_someone_else_wrote_is_left_alone(setup):
    _, manifest, installer = setup
    manifest.parent.mkdir(parents=True)
    manifest.write_text('[workspace]\nname = "mine"\n\n[dependencies]\nnumpy = "*"\n')
    for reinstall in (False, True):
        with pytest.raises(InvalidData) as e:
            installer.get_install_command(['cmake'], reinstall=reinstall)
        assert 'not written by rosdep' in str(e.value)
    assert 'name = "mine"' in manifest.read_text()
    # Detection treats it as holding nothing, and does not raise.
    assert conda.conda_detect(['cmake']) == []


def test_constraints_come_from_beside_the_rosdistro_index(tmp_path, monkeypatch):
    conda._constraints_cache.clear()
    monkeypatch.delenv(conda.CONSTRAINTS_ENV, raising=False)
    monkeypatch.setenv('ROS_DISTRO', 'rolling')
    monkeypatch.setenv('ROSDISTRO_INDEX_URL', 'https://example.invalid/rosdistro/index-v4.yaml')
    assert conda.constraints_location() == 'https://example.invalid/rosdistro/rolling/conda/constraints.yaml'

    # A file:// index works the same way, and is read.
    (tmp_path / 'rolling' / 'conda').mkdir(parents=True)
    (tmp_path / 'rolling' / 'conda' / 'constraints.yaml').write_text(CONSTRAINTS)
    monkeypatch.setenv('ROSDISTRO_INDEX_URL', (tmp_path / 'index-v4.yaml').as_uri())
    assert conda.load_constraints().pins['cmake'] == '3.31.*'
    conda._constraints_cache.clear()

    monkeypatch.delenv('ROS_DISTRO')
    with pytest.raises(InvalidData):
        conda.constraints_location()
    monkeypatch.setenv('ROS_DISTRO', 'rolling')
    monkeypatch.delenv('ROSDISTRO_INDEX_URL')
    with pytest.raises(InvalidData):
        conda.constraints_location()


def test_unreadable_constraints_are_reported(tmp_path, monkeypatch):
    conda._constraints_cache.clear()
    monkeypatch.setenv(conda.CONSTRAINTS_ENV, str(tmp_path / 'missing.yaml'))
    with pytest.raises(InvalidData) as e:
        CondaInstaller().resolve(['cmake'])
    assert 'missing.yaml' in str(e.value)


def test_manifest_values_cannot_break_out_of_their_strings(setup):
    _, manifest, _ = setup
    constraints = Constraints(['conda-forge'], {}, {'cmake': '3.*'})
    conda.write_manifest(str(manifest), constraints, {'cmake': '3.*'})
    assert os.path.exists(str(manifest))
    assert dependencies(manifest) == {'cmake': '3.*'}
    # Whatever is written is one quoted string per value.
    for line in manifest.read_text().splitlines():
        if ' = ' in line and not line.startswith(('name', 'channels', 'platforms')):
            assert line.count('"') == 4, line
