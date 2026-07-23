package test_repo;

/**
 * Fixture class for the code map export integration test.
 */
public class CodeMapFixture {
    private final CodeMapHelper helper = new CodeMapHelper();

    /**
     * Performs a deterministic calculation.
     *
     * @param left left operand
     * @param right right operand
     * @return the calculated value
     */
    public int calculate(int left, int right) {
        return helper.normalize(left + right);
    }
}
