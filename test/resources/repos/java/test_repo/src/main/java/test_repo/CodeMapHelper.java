package test_repo;

/**
 * Helper used by {@link CodeMapFixture} in the code map export integration test.
 */
public class CodeMapHelper {
    /**
     * Normalizes a value.
     *
     * @param value the raw value
     * @return the normalized value
     */
    public int normalize(int value) {
        return Math.max(0, value);
    }
}
