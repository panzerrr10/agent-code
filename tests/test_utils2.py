from demos.utils2 import add, multiply, subtract


class TestAdd:
    def test_positive(self):
        assert add(1, 2) == 3

    def test_negative(self):
        assert add(-1, -2) == -3

    def test_mixed(self):
        assert add(-5, 5) == 0

    def test_float(self):
        assert add(1.5, 2.5) == 4.0


class TestSubtract:
    def test_positive(self):
        assert subtract(5, 3) == 2

    def test_negative_result(self):
        assert subtract(3, 5) == -2

    def test_mixed(self):
        assert subtract(-5, 5) == -10

    def test_float(self):
        assert subtract(2.5, 1.5) == 1.0


class TestMultiply:
    def test_positive(self):
        assert multiply(3, 4) == 12

    def test_by_zero(self):
        assert multiply(5, 0) == 0

    def test_negative(self):
        assert multiply(-2, 3) == -6

    def test_float(self):
        assert multiply(1.5, 2) == 3.0
