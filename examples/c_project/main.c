#include <stdio.h>
#include <stddef.h>

/* Sum an array of integers. */
static int sum_ints(const int *values, size_t count)
{
    int total = 0;
    for (size_t i = 0; i < count; i++) {
        total += values[i];
    }
    return total;
}

int main(void)
{
    const int data[] = {1, 2, 3, 4, 5};
    const size_t n = sizeof(data) / sizeof(data[0]);

    printf("count = %zu\n", n);
    printf("sum   = %d\n", sum_ints(data, n));
    return 0;
}
