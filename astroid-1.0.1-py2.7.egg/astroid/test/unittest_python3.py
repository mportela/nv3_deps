# copyright 2003-2013 LOGILAB S.A. (Paris, FRANCE), all rights reserved.
# contact http://www.logilab.fr/ -- mailto:contact@logilab.fr
#
# This file is part of astroid.
#
# astroid is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by the
# Free Software Foundation, either version 2.1 of the License, or (at your
# option) any later version.
#
# astroid is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License
# for more details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with astroid. If not, see <http://www.gnu.org/licenses/>.
import sys

from logilab.common.testlib import TestCase, unittest_main, require_version

from astroid.node_classes import Assign
from astroid.manager import AstroidManager
from astroid.builder import AstroidBuilder


class Python3TC(TestCase):
    def setUp(self):
        self.manager = AstroidManager()
        self.builder = AstroidBuilder(self.manager)
        self.manager.astroid_cache.clear()

    @require_version('3.0')
    def test_starred_notation(self):
        astroid = self.builder.string_build("*a, b = [1, 2, 3]", 'test', 'test')

        # Get the star node
        node = next(next(next(astroid.get_children()).get_children()).get_children())

        self.assertTrue(isinstance(node.ass_type(), Assign))

if __name__ == '__main__':
    unittest_main()
